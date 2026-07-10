import type { Settings } from "./types";
import { browserLocale, resolveLocale } from "../i18n";

const DEFAULT_FAST_MODEL_VARIANTS = ["gpt-5.5", "gpt-5.4"];
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
  gateway_request_timeout_seconds: 300,
  gateway_auto_retry_enabled: true,
  gateway_auto_retry_max_attempts: 30,
  gateway_image_proxy_enabled: false,
  gateway_image_proxy_model: "",
  gateway_fast_model_variants: DEFAULT_FAST_MODEL_VARIANTS,
  official_disabled_models: [],
  official_model_sort_order: [],
  official_provider_sort_order: 0,
  proxy_port: 9099,
};

type LegacySettings = Partial<Settings> & {
  auto_start_proxy?: boolean;
};

export function normalizeSettings(settings: LegacySettings | null | undefined): Settings {
  const source = settings ?? {};
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
  };
}

export function normalizeOfficialModelId(value: string): string {
  value = value.trim();
  return value.startsWith("openai/gpt-") ? value.slice("openai/".length) : value;
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
  return normalized.length ? normalized : [...DEFAULT_FAST_MODEL_VARIANTS];
}
