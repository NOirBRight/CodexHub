export interface Model {
  id: string;
  display_name?: string | null;
  upstream_model?: string | null;
  source_kind?: string | null;
  locked?: boolean;
  codex_enabled?: boolean;
  gateway_exported?: boolean;
  context_window?: number | null;
  max_output_tokens?: number | null;
  input_modalities?: string[] | null;
  supported_reasoning_levels?: string[] | null;
  default_reasoning_level?: string | null;
  pricing?: ModelPricing | null;
  metadata_provenance?: MetadataProvenance | null;
  sort_order?: number | null;
  enabled: boolean;
}

export interface ModelPricing {
  input_per_million?: number | null;
  cached_input_per_million?: number | null;
  output_per_million?: number | null;
  currency: string;
  source: string;
  estimate: boolean;
}

export interface MetadataProvenance {
  source: string;
  source_url?: string | null;
  fetched_at?: string | null;
  confidence: string;
}

export type UpstreamFormat = "auto" | "responses" | "chat_completions" | "anthropic_messages";
export type ToolProtocol = "auto" | "responses_structured" | "chat_tools" | "text_compat" | "none";

export interface Provider {
  id: string;
  name: string;
  base_url: string;
  api_key?: string | null;
  upstream_format?: UpstreamFormat | null;
  available_upstream_formats?: UpstreamFormat[] | null;
  tool_protocol?: ToolProtocol | null;
  display_prefix?: string | null;
  sort_order?: number | null;
  enabled: boolean;
  locked?: boolean;
  models: Model[];
}

export interface UpstreamFormatProbeResult {
  base_url: string;
  model: string | null;
  models_ok: boolean;
  responses_text_ok: boolean;
  responses_tool_ok: boolean;
  responses_tool_stream_ok: boolean;
  chat_text_ok: boolean;
  chat_tool_ok: boolean;
  chat_tool_stream_ok: boolean;
  chat_tool_history_ok: boolean;
  anthropic_text_ok: boolean;
  recommended_format: UpstreamFormat;
  recommended_tool_protocol: ToolProtocol;
  notes: string[];
  duration_ms?: number | null;
}

export interface ModelEndpointTestResult {
  ok: boolean;
  upstream_format: UpstreamFormat;
  endpoint: string;
  status: number;
}

export interface AppStatus {
  mode: string;
  proxy_running: boolean;
  proxy_port: number;
  proxy_build?: string | null;
  message: string;
  history_sync_status?: string | null;
  history_sync_message?: string | null;
}

export interface GatewayStatus {
  proxy_running: boolean;
  host: string;
  port: number;
  build?: string | null;
  features: string[];
  has_chat_completions_gateway: boolean;
  codex_auth: CodexAuthStatus;
  endpoints: GatewayEndpoints;
  official_models: GatewayModel[];
  diagnostics: GatewayDiagnostic[];
}

export interface CodexAuthStatus {
  auth_file_present: boolean;
  logged_in: boolean;
  auth_mode?: string | null;
  account_id_present: boolean;
  access_token_present: boolean;
  refresh_token_present: boolean;
  token_refresh_status: string;
  last_refresh?: string | null;
  issue?: string | null;
}

export interface GatewayEndpoints {
  base_url: string;
  models: string;
  responses: string;
  chat_completions: string;
}

export interface GatewayModel {
  id: string;
  display_name: string;
  source: string;
  source_kind: string;
  supports_responses: boolean;
  supports_chat_completions: boolean;
  context_window: number;
}

export interface GatewayUsageSummary {
  requests: number;
  successful_requests: number;
  missing_usage_requests: number;
  total_tokens?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cached_input_tokens?: number | null;
  cache_hit_rate?: number | null;
  estimated_cost_usd?: number | null;
  cost_label: string;
}

export interface GatewayUsageEvent {
  ts?: string | null;
  request_id?: string | null;
  model?: string | null;
  upstream?: string | null;
  status?: number | null;
  duration_ms?: number | null;
  usage_source: string;
  usage_missing_reason?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  cached_input_tokens?: number | null;
  reasoning_tokens?: number | null;
}

export interface TelemetryStatus {
  event_log_size: number;
  indexed_offset: number;
  lag_bytes: number;
  backfill_pending: boolean;
  last_indexed_at?: string | null;
  last_error?: string | null;
}

export interface GatewayUsageSnapshot {
  summary: GatewayUsageSummary;
  events: GatewayUsageEvent[];
  telemetry_status: TelemetryStatus;
}

export interface UsageQueryWindow {
  startTs?: string | null;
  endTs?: string | null;
}

export interface GatewayDiagnostic {
  level: "ok" | "warning" | "error" | string;
  category: string;
  message: string;
}

export type GatewayTestKind =
  | "health"
  | "models"
  | "chat_completions"
  | "chat_completions_stream"
  | "responses_stream";

export interface GatewayTestResult {
  ok: boolean;
  kind: string;
  endpoint: string;
  method: string;
  model?: string | null;
  status?: number | null;
  latency_ms: number;
  first_token_ms?: number | null;
  sanitized_body?: string | null;
  error?: string | null;
}

export interface GatewayClientConfig {
  base_url: string;
  api_key: string;
  model: string;
  json: string;
  curl_test: string;
}

export interface GatewayClientInfo {
  id: string;
  name: string;
  kind: string;
  installed: boolean;
  auto_apply_supported: boolean;
  config_path?: string | null;
  route_mode: string;
  status: string;
  current_version?: string | null;
  latest_version?: string | null;
}

export interface GatewayClientConfigPreview {
  client_id: string;
  can_apply: boolean;
  strategy: string;
  config_path?: string | null;
  current_redacted?: string | null;
  next_redacted: string;
  backup_required: boolean;
  message: string;
}

export interface GatewayClientApplyResult {
  client_id: string;
  applied: boolean;
  config_path?: string | null;
  backup_path?: string | null;
  message: string;
}

export interface GatewayEvent {
  ts?: string | null;
  event?: string | null;
  request_id?: string | null;
  path?: string | null;
  method?: string | null;
  model?: string | null;
  upstream?: string | null;
  upstream_format?: string | null;
  inbound_format?: string | null;
  route_reason?: string | null;
  status?: number | null;
  duration_ms?: number | null;
  error?: string | null;
  detail?: string | null;
  category: string;
}

export interface SubagentMatrixStatus {
  readiness: SubagentReadiness[];
  rows: SubagentMatrixRow[];
  recent_events: GatewayEvent[];
  message: string;
}

export interface SubagentReadiness {
  step: string;
  ready: boolean;
  feature: string;
}

export interface SubagentMatrixRow {
  model: string;
  provider: string;
  thread_id?: string | null;
  child_agent_id?: string | null;
  wait_timed_out?: boolean | null;
  close_succeeded?: boolean | null;
  child_output_ok?: boolean | null;
  status: string;
  detail: string;
}

export interface Settings {
  locale: "zh-CN" | "en-US";
  auto_sync_history: boolean;
  unified_codex_history: boolean;
  auto_start_proxy: boolean;
  include_official_models: boolean;
  auto_sync_catalog: boolean;
  auto_sync_clients: boolean;
  default_codex_route: string;
  gateway_bind_address: string;
  gateway_client_key: string;
  gateway_enable_models: boolean;
  gateway_enable_responses: boolean;
  gateway_enable_chat_completions: boolean;
  gateway_request_timeout_seconds: number;
  gateway_auto_retry_enabled: boolean;
  gateway_auto_retry_max_attempts: number;
  gateway_image_proxy_enabled: boolean;
  gateway_image_proxy_model: string;
  gateway_fast_model_variants: string[];
  official_disabled_models: string[];
  official_model_sort_order: string[];
  official_provider_sort_order: number;
  proxy_port: number;
}

export type TabId = "codexhub" | "gateway";

export type GatewayClientId = "opencode" | "zcode" | "pi" | "omp";

export interface GatewayClientContract {
  id: GatewayClientId;
  name: string;
  kind?: string;
  description?: string;
  config_path: string;
}

export interface GatewayClientSyncItem {
  client_id: string;
  name: string;
  status: "applied" | "skipped" | "failed" | string;
  applied: boolean;
  skipped: boolean;
  message: string;
  config_path?: string | null;
  backup_path?: string | null;
}

export interface GatewayClientSyncSummary {
  applied: number;
  skipped: number;
  failed: number;
  results: GatewayClientSyncItem[];
  message: string;
}
