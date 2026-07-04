import type { Settings } from "./types";

const DEFAULT_FAST_MODEL_VARIANTS = ["openai/gpt-5.5", "openai/gpt-5.4"];

const DEFAULT_SETTINGS: Settings = {
  auto_sync_history: false,
  unified_codex_history: true,
  auto_start_proxy: true,
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

export function normalizeSettings(settings: Partial<Settings> | null | undefined): Settings {
  const source = settings ?? {};
  return {
    ...DEFAULT_SETTINGS,
    ...source,
    unified_codex_history: source.unified_codex_history ?? DEFAULT_SETTINGS.unified_codex_history,
    auto_sync_clients:
      source.auto_sync_clients ??
      source.auto_sync_catalog ??
      DEFAULT_SETTINGS.auto_sync_clients,
    gateway_fast_model_variants: source.gateway_fast_model_variants?.length
      ? source.gateway_fast_model_variants
      : DEFAULT_FAST_MODEL_VARIANTS,
    official_disabled_models: source.official_disabled_models ?? [],
    official_model_sort_order: source.official_model_sort_order ?? [],
  };
}
