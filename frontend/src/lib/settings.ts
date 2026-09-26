import type { Settings } from "./types";
import { browserLocale, resolveLocale } from "../i18n";
import officialFastVariants from "../../../config/official_fast_variants.json";

const DEFAULT_FAST_MODEL_VARIANTS = Object.values(officialFastVariants).sort();
const ALLOWED_FAST_MODEL_VARIANTS = new Set(DEFAULT_FAST_MODEL_VARIANTS);

const DEFAULT_SETTINGS: Settings = {
  locale: browserLocale(),
  auto_sync_history: false,
  unified_codex_history: true,
  auto_start_software: true,
  auto_start_gateway: true,
  include_official_models: true,
  auto_sync_catalog: true,
  auto_sync_clients: true,
  default_codex_route: "hub",
  gateway_bind_address: "127.0.0.1",
  gateway_client_key: "codexhub-proxy",
  gateway_enable_models: true,
  gateway_enable_responses: true,
  gateway_enable_chat_completions: true,
  gateway_request_timeout_seconds: 600,
  gateway_auto_retry_enabled: true,
  gateway_auto_retry_max_attempts: 30,
  gateway_image_proxy_enabled: false,
  gateway_image_proxy_model: "",
  gateway_fast_model_variants: DEFAULT_FAST_MODEL_VARIANTS,
  official_disabled_models: [],
  official_model_sort_order: [],
  official_provider_sort_order: 0,
  codex_default_subagent_model: "",
  codex_default_subagent_reasoning_effort: "",
  opencode_default_subagent_model: "",
  opencode_default_subagent_reasoning_effort: "",
  zcode_default_subagent_model: "",
  zcode_default_subagent_reasoning_effort: "",
  omp_default_subagent_model: "",
  omp_default_subagent_reasoning_effort: "",
  grok_default_subagent_model: "",
  grok_default_subagent_reasoning_effort: "",
  proxy_port: 9099,
};

type LegacySettings = Partial<Settings> & {
  auto_start_proxy?: boolean;
  openai_context_guard_enabled?: boolean;
};

export function normalizeSettings(settings: LegacySettings | null | undefined): Settings {
  const source = { ...settings };
  // Retired setting must not be written back by older persisted preferences.
  delete source.openai_context_guard_enabled;
  return {
    ...DEFAULT_SETTINGS,
    ...source,
    locale: source.locale ? resolveLocale(source.locale) : DEFAULT_SETTINGS.locale,
    unified_codex_history: source.unified_codex_history ?? DEFAULT_SETTINGS.unified_codex_history,
    auto_start_software:
      source.auto_start_software ??
      source.auto_start_proxy ??
      DEFAULT_SETTINGS.auto_start_software,
    auto_start_gateway: source.auto_start_gateway ?? DEFAULT_SETTINGS.auto_start_gateway,
    auto_sync_clients:
      source.auto_sync_clients ??
      source.auto_sync_catalog ??
      DEFAULT_SETTINGS.auto_sync_clients,
    gateway_fast_model_variants: normalizeFastModelVariants(source.gateway_fast_model_variants),
    official_disabled_models: normalizeModelIds(source.official_disabled_models),
    official_model_sort_order: normalizeModelIds(source.official_model_sort_order),
    ...defaultSubagentSettings(source),
  };
}

function defaultSubagentSettings(source: LegacySettings) {
  return {
    ...normalizeSubagentPair(
      "codex_default_subagent_model",
      "codex_default_subagent_reasoning_effort",
      source.codex_default_subagent_model,
      source.codex_default_subagent_reasoning_effort,
    ),
    ...normalizeSubagentPair(
      "opencode_default_subagent_model",
      "opencode_default_subagent_reasoning_effort",
      source.opencode_default_subagent_model,
      source.opencode_default_subagent_reasoning_effort,
    ),
    ...normalizeSubagentPair(
      "zcode_default_subagent_model",
      "zcode_default_subagent_reasoning_effort",
      source.zcode_default_subagent_model,
      source.zcode_default_subagent_reasoning_effort,
    ),
    ...normalizeSubagentPair(
      "omp_default_subagent_model",
      "omp_default_subagent_reasoning_effort",
      source.omp_default_subagent_model,
      source.omp_default_subagent_reasoning_effort,
    ),
    ...normalizeSubagentPair(
      "grok_default_subagent_model",
      "grok_default_subagent_reasoning_effort",
      source.grok_default_subagent_model,
      source.grok_default_subagent_reasoning_effort,
    ),
  };
}

function normalizeSubagentPair<M extends string, E extends string>(
  modelKey: M,
  effortKey: E,
  model: string | null | undefined,
  effort: string | null | undefined,
) {
  const normalized = normalizeDefaultSubagentModel(model);
  return {
    [modelKey]: normalized,
    [effortKey]: normalized ? normalizeDefaultSubagentEffort(effort) : "",
  } as Record<M | E, string>;
}

function normalizeDefaultSubagentModel(value: string | null | undefined) {
  const normalized = (value ?? "").trim();
  if (!normalized) return "";
  return normalizeOfficialModelId(normalized) ?? normalized;
}

const ALLOWED_SUBAGENT_EFFORTS = new Set([
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
]);

function normalizeDefaultSubagentEffort(value: string | null | undefined) {
  const effort = (value ?? "").trim().toLowerCase();
  return ALLOWED_SUBAGENT_EFFORTS.has(effort) ? effort : "";
}

export function normalizeOfficialModelId(
  value: string,
  knownOfficialIds: ReadonlySet<string> = new Set([
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
  ]),
): string | null {
  value = value.trim();
  if (value.startsWith("openai/gpt-")) {
    const bare = value.slice("openai/".length);
    return knownOfficialIds.has(bare) ? bare : null;
  }
  return value;
}

function normalizeModelIds(values: string[] | null | undefined) {
  const output: string[] = [];
  for (const value of values ?? []) {
    const normalized = normalizeOfficialModelId(value);
    if (normalized && !output.includes(normalized)) {
      output.push(normalized);
    }
  }
  return output;
}

function normalizeFastModelVariants(values: string[] | null | undefined) {
  const source = values?.length ? values : DEFAULT_FAST_MODEL_VARIANTS;
  const normalized = normalizeModelIds(source).filter((value) => ALLOWED_FAST_MODEL_VARIANTS.has(value));
  if (
    (normalized.length === 2 && normalized.every((value) => value === "gpt-5.5" || value === "gpt-5.4")) ||
    (normalized.length === 6 && normalized.every((value) => value !== "gpt-6-luna" && value !== "gpt-6-sol"))
  ) {
    return [...DEFAULT_FAST_MODEL_VARIANTS];
  }
  return normalized.length ? normalized : [...DEFAULT_FAST_MODEL_VARIANTS];
}
