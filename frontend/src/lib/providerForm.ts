import type { Model, ToolProtocol, UpstreamFormat } from "./types";

export const emptyProvider = {
  id: "",
  name: "",
  base_url: "",
  api_key: "",
  upstream_format: "responses" as UpstreamFormat,
  available_upstream_formats: [] as UpstreamFormat[],
  tool_protocol: "auto" as ToolProtocol,
  display_prefix: "",
  models: [] as Model[],
};

export type AddProviderForm = typeof emptyProvider;

export type InlineTestState = "idle" | "testing" | "success" | "error";

export const endpointSelectionOptions: Array<{ value: UpstreamFormat; labelKey: string }> = [
  { value: "responses", labelKey: "providers.upstreamFormats.responses" },
  { value: "chat_completions", labelKey: "providers.upstreamFormats.chatCompletions" },
  { value: "anthropic_messages", labelKey: "providers.upstreamFormats.anthropicMessages" },
];

export const reasoningLevelOptions = ["low", "medium", "high", "xhigh", "max"];
