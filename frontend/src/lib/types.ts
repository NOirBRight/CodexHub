export interface Model {
  id: string;
  display_name?: string | null;
  upstream_model?: string | null;
  tool_surface_strategy?: ToolSurfaceStrategy | null;
  source_kind?: string | null;
  locked?: boolean;
  codex_enabled?: boolean;
  gateway_exported?: boolean;
  context_window?: number | null;
  max_context_window?: number | null;
  effective_source?: string | null;
  max_source?: string | null;
  confidence?: string | null;
  verified_at?: string | null;
  max_output_tokens?: number | null;
  input_modalities?: string[] | null;
  supported_reasoning_levels?: string[] | null;
  default_reasoning_level?: string | null;
  pricing?: ModelPricing | null;
  metadata_provenance?: MetadataProvenance | null;
  sort_order?: number | null;
  enabled: boolean;
}

export interface OfficialRefreshResult {
  models: Model[];
  restart_required: boolean;
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
export type ToolSurfaceStrategy = "eager" | "deferred_core";

export interface Provider {
  id: string;
  name: string;
  base_url: string;
  api_key?: string | null;
  upstream_format?: UpstreamFormat | null;
  available_upstream_formats?: UpstreamFormat[] | null;
  tool_protocol?: ToolProtocol | null;
  tool_surface_strategy?: ToolSurfaceStrategy | null;
  reports_cached_input_tokens?: boolean | null;
  supports_developer_role?: boolean | null;
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
  gateway_lifecycle: "unavailable" | "stopped" | "starting" | "running" | "stopping" | "restarting" | "failed";
  history_sync_status?: string | null;
  history_sync_message?: string | null;
}

export interface UnifiedHistoryResult {
  status: "clean" | "repaired" | "deferred" | "restart_required" | "conflict";
  changed_rows: number;
  changed_files: number;
  backup_path?: string | null;
  receipt_path?: string | null;
  reason?: string | null;
  error?: string | null;
  codex_restarted: boolean;
}

export interface AppVersionInfo {
  current_version: string;
}

export type BuildFlavor = "normal" | "debug";
export type RoutingOwner = "official" | "release" | "beta" | "unknown_external";

export interface BuildInfo {
  semantic_version: string;
  flavor: BuildFlavor;
  source_revision: string;
  diagnostics_enabled: boolean;
}

export interface AppFlavorInfo {
  build: BuildInfo;
  routing_owner: RoutingOwner;
  product_name: string;
  bridge_port: number;
  gateway_port: number;
  default_codex_home_suffix: string;
  runtime_home_suffix: string;
  codex_target_home_suffix: string;
  codex_target_owner: RoutingOwner | null;
  codex_takeover_required: boolean;
}

export interface AppUpdateStatus {
  available: boolean;
  current_version: string;
  latest_version?: string | null;
  checked_at: string;
  notes?: string | null;
  date?: string | null;
}

export interface AppUpdateInstallResult {
  installed: boolean;
  version: string;
  message: string;
}

export type AppUpdateInstallPhase =
  | "idle"
  | "checking"
  | "downloading"
  | "installing"
  | "restarting"
  | "failed";

export interface AppUpdateInstallStatus {
  phase: AppUpdateInstallPhase;
  current_version: string;
  target_version?: string | null;
  downloaded_bytes: number;
  total_bytes?: number | null;
  message: string;
  updated_at: string;
}

export interface AppUpdateCompletionStatus {
  completed: boolean;
  current_version: string;
  target_version: string;
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

export interface DiagnosticsStatus {
  active: boolean;
  paused: boolean;
  flavor: "debug";
  rolling_bytes: number;
  rolling_window_seconds: number;
  incident_count: number;
  incident_ids: string[];
  last_marker_category?: string | null;
  last_marker_at_ms?: number | null;
  rolling_evicted_segments: number;
  incident_evicted_count: number;
  truncated: boolean;
  schema_version: number;
  writer_failure_count: number;
  writer_queue_dropped_records: number;
}

export interface DiagnosticsActionResult {
  status: DiagnosticsStatus;
  accepted?: boolean | null;
  incident_id?: string | null;
  deleted?: boolean | null;
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
  input_modalities?: string[] | null;
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
  client_id?: string | null;
  client_inference_source?: string | null;
  reports_cached_input_tokens?: boolean | null;
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

export interface OpenAIUsageQueryWindow {
  startTime?: number | null;
  endTime?: number | null;
  forceRefresh?: boolean | null;
}

export interface OpenAIUsageBucket {
  date: string;
  start_time: number;
  end_time: number;
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  input_cached_tokens: number;
  num_model_requests: number;
}

export interface OpenAIUsageLimit {
  key: string;
  name: string;
  period: string;
  limit?: number | null;
  used?: number | null;
  remaining?: number | null;
  resets_at?: string | null;
}

export interface OpenAIUsageSnapshot {
  start_time: number;
  end_time: number;
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  input_cached_tokens: number;
  num_model_requests: number;
  peak_daily_tokens?: number | null;
  longest_running_turn_sec?: number | null;
  current_streak_days?: number | null;
  longest_streak_days?: number | null;
  limits: OpenAIUsageLimit[];
  buckets: OpenAIUsageBucket[];
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

export type GatewayClientRouteMode =
  | "official"
  | "release"
  | "beta"
  | "hub"
  | "stale"
  | "other_channel"
  | "unknown";

export interface GatewayClientInfo {
  id: string;
  name: string;
  kind: string;
  installed: boolean;
  auto_apply_supported: boolean;
  config_path?: string | null;
  route_mode: GatewayClientRouteMode;
  route_owner: RoutingOwner;
  route_endpoint?: string | null;
  managed_by_current_app: boolean;
  status: string;
  versions_checked?: boolean | null;
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

export interface CodexHubError {
  code: string;
  message: string;
  source: string;
  retryable: boolean;
  details?: Record<string, unknown> | null;
}

export interface GatewayEvent {
  ts?: string | null;
  event?: string | null;
  request_id?: string | null;
  client_request_id?: string | null;
  query_id?: string | null;
  session_id?: string | null;
  client_id?: string | null;
  path?: string | null;
  method?: string | null;
  model?: string | null;
  upstream?: string | null;
  provider_id?: string | null;
  upstream_format?: string | null;
  inbound_format?: string | null;
  request_kind?: string | null;
  route_reason?: string | null;
  route_mode?: string | null;
  failure_class?: string | null;
  retryable?: boolean | null;
  attempt?: number | null;
  max_attempts?: number | null;
  delay_ms?: number | null;
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
  auto_start_software: boolean;
  auto_start_gateway: boolean;
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
  openai_context_guard_enabled: boolean;
  gateway_fast_model_variants: string[];
  official_disabled_models: string[];
  official_model_sort_order: string[];
  official_provider_sort_order: number;
  proxy_port: number;
}

export interface CodexContextGuardStatus {
  enabled: boolean;
  codex_enabled: boolean;
  gateway_enabled: boolean;
  model_context_window?: number | null;
  model_auto_compact_token_limit?: number | null;
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
