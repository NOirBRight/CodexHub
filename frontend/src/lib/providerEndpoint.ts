import i18n from "../i18n";
import { messageFromError } from "./tauri";
import type { Model, Provider, ToolProtocol, UpstreamFormat, UpstreamFormatProbeResult } from "./types";

type Translate = (key: string, options?: Record<string, unknown>) => string;

type EndpointProbeDraft = {
  upstream_format?: UpstreamFormat | null;
  available_upstream_formats?: UpstreamFormat[] | null;
  tool_protocol?: ToolProtocol | null;
};

const ENDPOINT_FORMAT_ORDER: UpstreamFormat[] = [
  "responses",
  "chat_completions",
  "anthropic_messages",
];

export function normalizeProviderEndpointSelection(provider: Provider): Provider {
  return {
    ...provider,
    upstream_format:
      !provider.upstream_format || provider.upstream_format === "auto"
        ? "responses"
        : provider.upstream_format,
    available_upstream_formats: normalizeEndpointFormats(provider.available_upstream_formats),
    tool_protocol: provider.tool_protocol ?? "auto",
  };
}

export function normalizeEndpointFormats(values?: Array<UpstreamFormat | null | undefined> | null): UpstreamFormat[] {
  if (!values?.length) {
    return [];
  }
  const available = new Set(values.filter((value): value is UpstreamFormat => Boolean(value)));
  return ENDPOINT_FORMAT_ORDER.filter((value) => available.has(value));
}

export function mergeEndpointFormats(
  ...groups: Array<Array<UpstreamFormat | null | undefined> | null | undefined>
): UpstreamFormat[] {
  return normalizeEndpointFormats(groups.flatMap((group) => group ?? []));
}

export function hasAvailableEndpointFormats(values?: Array<UpstreamFormat | null | undefined> | null) {
  return normalizeEndpointFormats(values).length > 0;
}

export function probeDetectedEndpointFormat(result: UpstreamFormatProbeResult): UpstreamFormat | null {
  return normalizedProbeEndpointFormat(result.recommended_format) ?? probeAvailableFormats(result)[0] ?? null;
}

export function normalizedProbeEndpointFormat(value?: string | null): UpstreamFormat | null {
  const normalized = value?.trim().toLowerCase().replace(/[-\s]+/g, "_");
  if (!normalized || normalized === "auto") {
    return null;
  }
  if (normalized === "responses" || normalized === "response") {
    return "responses";
  }
  if (normalized === "chat_completions" || normalized === "chat_completion" || normalized === "chat") {
    return "chat_completions";
  }
  if (normalized === "anthropic_messages" || normalized === "anthropic_message" || normalized === "anthropic") {
    return "anthropic_messages";
  }
  return null;
}

export function applyProviderProbeResult(provider: Provider, result: UpstreamFormatProbeResult): Provider {
  const detectedFormat = probeDetectedEndpointFormat(result);
  return {
    ...provider,
    upstream_format: detectedFormat ?? provider.upstream_format,
    available_upstream_formats: probeAvailableFormats(result),
    tool_protocol: result.recommended_tool_protocol,
  };
}

export function applyAddProviderProbeResult<TDraft extends EndpointProbeDraft>(form: TDraft, result: UpstreamFormatProbeResult): TDraft {
  const detectedFormat = probeDetectedEndpointFormat(result);
  return {
    ...form,
    upstream_format: detectedFormat ?? form.upstream_format,
    available_upstream_formats: probeAvailableFormats(result),
    tool_protocol: result.recommended_tool_protocol,
  };
}

export function normalizedEndpointFormat(value?: UpstreamFormat | null): UpstreamFormat {
  if (value === "chat_completions" || value === "anthropic_messages") {
    return value;
  }
  return "responses";
}

export function upstreamFormatLabel(value?: UpstreamFormat | null, t?: Translate) {
  if (value === "responses") {
    return t?.("providers.upstreamFormats.responses") ?? i18n.t("providers.upstreamFormats.responses");
  }
  if (value === "chat_completions") {
    return t?.("providers.upstreamFormats.chatCompletions") ?? i18n.t("providers.upstreamFormats.chatCompletions");
  }
  if (value === "anthropic_messages") {
    return t?.("providers.upstreamFormats.anthropicMessages") ?? i18n.t("providers.upstreamFormats.anthropicMessages");
  }
  return t?.("providers.upstreamFormats.responses") ?? i18n.t("providers.upstreamFormats.responses");
}

export function toolProtocolLabel(value?: ToolProtocol | null) {
  if (value === "responses_structured") {
    return "Structured Responses tools";
  }
  if (value === "chat_tools") {
    return "Chat tool calls";
  }
  if (value === "text_compat") {
    return "Gateway compatibility";
  }
  if (value === "none") {
    return "Tools unavailable";
  }
  return "Auto tools";
}

export function probeAvailableFormats(result?: UpstreamFormatProbeResult | null): UpstreamFormat[] {
  if (!result) {
    return [];
  }
  const formats: UpstreamFormat[] = [];
  if (result.responses_text_ok || result.responses_tool_ok || result.responses_tool_stream_ok) {
    formats.push("responses");
  }
  if (result.chat_text_ok || result.chat_tool_ok || result.chat_tool_stream_ok) {
    formats.push("chat_completions");
  }
  if (result.anthropic_text_ok) {
    formats.push("anthropic_messages");
  }
  const recommendedFormat = normalizedProbeEndpointFormat(result.recommended_format);
  if (recommendedFormat && !formats.includes(recommendedFormat)) {
    formats.push(recommendedFormat);
  }
  return formats;
}

export function probeResultSummary(result: UpstreamFormatProbeResult) {
  const availableFormats = probeAvailableFormats(result).map((format) => upstreamFormatLabel(format));
  if (availableFormats.length) {
    return i18n.t("providers.availableFormats", { formats: availableFormats.join(", ") });
  }
  return i18n.t("providers.recommendedFormat", { format: upstreamFormatLabel(result.recommended_format) });
}

export function modelProbeId(model: Model) {
  return model.upstream_model?.trim() || model.id;
}

export function shortProviderDiscoveryError(err: unknown, t: Translate) {
  const message = messageFromError(err);
  const missingEnv = message.match(/\b([A-Z_][A-Z0-9_]*_API_KEY)\b[^.]*\bis not set\b/i);
  if (missingEnv) {
    return t("providers.discoveryFailedNotSet", { env: missingEnv[1] });
  }
  if (/unauthorized|401/i.test(message)) {
    return t("providers.discoveryFailedUnauthorized");
  }
  if (/timeout|timed out/i.test(message)) {
    return t("providers.discoveryTimedOut");
  }
  if (/not found|404/i.test(message)) {
    return t("providers.discoveryFailedMissingEndpoint");
  }
  if (/builder error|invalid/i.test(message)) {
    return t("providers.discoveryFailedInvalid");
  }
  return t("providers.discoveryFailed");
}
