// API 类型定义

export type ProxyRuntimeEgressMode = 'direct' | 'single_proxy'
export type ProxyRuntimeClearanceMode = 'none' | 'manual' | 'flaresolverr'

export interface ProxyRuntimeClearance {
  enabled: boolean
  mode: ProxyRuntimeClearanceMode
  cf_cookies: string
  cf_clearance: string
  has_cf_cookies?: boolean
  has_cf_clearance?: boolean
  user_agent: string
  browser: string
  flaresolverr_url: string
  timeout_sec: number
  refresh_interval: number
  warm_up_on_start: boolean
}

export interface ProxyRuntimeSettings {
  enabled: boolean
  egress_mode: ProxyRuntimeEgressMode
  proxy_url: string
  resource_proxy_url: string
  skip_ssl_verify: boolean
  reset_session_status_codes: number[]
  clearance: ProxyRuntimeClearance
}

export interface ProxyRuntimeStatus {
  enabled: boolean
  egress_mode: string
  proxy_source?: string
  has_proxy: boolean
  skip_ssl_verify?: boolean
  clearance_enabled: boolean
  clearance_mode: string
  has_clearance_bundle: boolean
  cached_clearance_hosts: string[]
}

export interface ClearanceTestResult {
  ok: boolean
  status: string
  latency_ms: number
  has_cookies: boolean
  user_agent: string
  error?: string | null
  runtime?: ProxyRuntimeStatus
}

export interface ImageErrorMessages {
  fallback: string
  quota: string
  no_account: string
  local_busy: string
  unsupported_model: string
  poll_timeout: string
  stream_interrupted: string
  connection_failed: string
  connection_timeout: string
  token_invalid: string
  text_reply: string
}

export interface Settings {
  proxy?: string
  fallback_proxy?: string
  proxy_runtime: ProxyRuntimeSettings
  base_url?: string
  refresh_account_interval_minute?: number
  image_retention_days?: number
  log_retention_days?: number
  image_poll_timeout_secs?: number
  image_stream_timeout_secs?: number
  image_poll_interval_secs?: number
  image_poll_initial_wait_secs?: number
  image_account_concurrency?: number
  image_parallel_generation?: boolean
  image_remove_conversation_after_result?: boolean
  image_error_friendly_enabled?: boolean
  image_error_messages: ImageErrorMessages
  image_settle_enabled?: boolean
  image_check_before_hit_enabled?: boolean
  image_settle_secs?: number
  image_timeout_retry_secs?: number
  auto_remove_invalid_accounts?: boolean
  auto_remove_rate_limited_accounts?: boolean
  log_levels: string[]
  global_system_prompt?: string
  sensitive_words?: string[]
  ai_review: {
    enabled: boolean
    base_url: string
    api_key: string
    model: string
    prompt: string
  }
  basic: {
    api_key?: string
    base_url?: string
    proxy?: string
    image_expire_hours?: number
  }
  image_generation: {
    enabled: boolean
    supported_models: string[]
    model_options?: string[]
    block_rich_output_on_base_chat_models?: boolean
    output_format?: 'base64' | 'url'
    nanobanana_lane?: 'fast' | 'thinking' | 'pro'
    nanobanana_lane_order?: Array<'fast' | 'thinking' | 'pro'>
  }
  model_catalog?: {
    models?: Array<{
      name: string
      display_name?: string
      lane?: string
      kind?: string
      tool_family?: string
      capabilities?: string[]
      endpoints?: string[]
      aliases?: string[]
      enabled?: boolean
      image_default?: boolean
      lane_order?: string[]
    }>
    chat_models?: string[]
    image_api_models?: string[]
    base_chat_models?: string[]
    specialized_chat_models?: string[]
    image_capable_chat_models?: string[]
  }
  quota_limits: {
    enabled: boolean
    fast_daily_limit: number
    thinking_daily_limit: number
    pro_daily_limit: number
    image_daily_limit: number
    music_daily_limit: number
    video_daily_limit: number
  }
  runtime_capacity: {
    uvicorn_workers: number
    text_concurrency_limit: number
    image_concurrency_limit: number
    request_queue_timeout_seconds: number
  }
  image_storage?: {
    enabled: boolean
    mode: 'local' | 'webdav' | 'both'
    webdav_url: string
    webdav_username: string
    webdav_password: string
    webdav_root_path: string
    public_base_url: string
  }
  backup?: {
    enabled: boolean
    provider: string
    account_id: string
    access_key_id: string
    secret_access_key: string
    bucket: string
    prefix: string
    interval_minutes: number
    rotation_keep: number
    encrypt: boolean
    passphrase: string
    include: Record<string, boolean>
  }
  chat_completion_cache?: {
    enabled: boolean
    ttl_seconds: number
    max_entries: number
    dedupe_inflight: boolean
    stream_cache: boolean
    normalize_messages: boolean
    drop_adjacent_duplicates: boolean
    drop_assistant_history: boolean
  }
  third_party_apps: {
    infinite_canvas: {
      enabled: boolean
      url: string
    }
  }
  proxy_profiles?: Array<{
    id: string
    name: string
    proxy: string
    no_proxy?: string
    enabled: boolean
    notes?: string
  }>
}

export interface SettingsUpdateResponse {
  status: string
  message?: string
  restart_required?: boolean
  config?: Settings
  runtime_capacity?: {
    uvicorn_workers: number
    text_concurrency_limit: number
    image_concurrency_limit: number
    request_queue_timeout_seconds: number
  }
}

export interface LogEntry {
  time: string
  level: 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL' | 'DEBUG'
  message: string
  row_id?: string
  req_id?: string
  tags?: string[]
  account_id?: string
  text?: string
  layer?: 'system' | 'chat' | 'reverse' | 'other'
  lane?: string
  model?: string
  kind?: string
  stage?: string
  served_label?: string
}

export interface AdminLogGroup {
  id: string
  row_ids: string[]
  status: 'success' | 'error' | 'timeout' | 'in_progress'
  account_id: string
  model: string
  lane: string
  terminal_kind: string
  started_at: string
  ended_at: string
  user_preview: string
  assistant_preview: string
  count: number
}

export interface LogsResponse {
  total: number
  limit: number
  logs: LogEntry[]
}

export interface AdminLogStats {
  memory: {
    total: number
    by_level: Record<string, number>
    capacity: number
  }
  active?: {
    source: 'file' | 'memory'
    total: number
  }
  errors: {
    count: number
    recent: LogEntry[]
  }
  chat_count: number
}

export interface AdminLogsResponse extends LogsResponse {
  filters?: {
    level?: string | null
    search?: string | null
    start_time?: string | null
    end_time?: string | null
  }
  groups?: AdminLogGroup[]
  stats: AdminLogStats
}

export interface AdminStatsTrend {
  labels: string[]
  total_requests: number[]
  success_requests?: number[]
  failed_requests: number[]
  rate_limited_requests: number[]
  model_requests?: Record<string, number[]>
  model_ttfb_times?: Record<string, number[]>
  model_total_times?: Record<string, number[]>
}

export interface AdminStats {
  total_accounts: number
  active_accounts: number
  abnormal_accounts: number
  disabled_accounts: number
  failed_accounts: number
  rate_limited_accounts: number
  idle_accounts: number
  total_quota: number
  unlimited_quota_count?: number
  unknown_quota_count?: number
  account_providers?: Record<string, DashboardProviderAccountStats>
  success_count?: number
  failed_count?: number
  recent_failures?: Array<{
    id?: string
    time?: string
    summary?: string
    endpoint?: string
    error_code?: string
    stage?: string
    reason?: string
    conversation_id?: string
  }>
  trend: AdminStatsTrend
}

export interface LoginRequest {
  password: string
}

export interface LoginResponse {
  ok: boolean
  authenticated: boolean
  version: string
  role?: string
  subject_id?: string
  name?: string
}

export interface AuthStatusResponse {
  ok: boolean
  authenticated: boolean
  version: string
  role?: string
  subject_id?: string
  name?: string
}

export interface VersionInfoResponse {
  version: string
  tag: string
  commit: string
}

export interface VersionCheckResponse extends VersionInfoResponse {
  repository: string
  latest_tag: string
  latest_version: string
  release_url: string
  is_latest: boolean
  update_available: boolean
  check_error?: string
}

export interface DashboardProviderAccountStats {
  total: number
  cumulative_total?: number
  active: number
  limited: number
  abnormal: number
  disabled: number
  total_quota: number
  unlimited_quota_count?: number
  unknown_quota_count?: number
  total_success?: number
  total_fail?: number
  by_type?: Record<string, number>
  source_available?: boolean
  healthy: boolean
}

export interface DashboardAccountStats extends DashboardProviderAccountStats {
  providers?: Record<string, DashboardProviderAccountStats>
}

export interface DashboardLogSummary {
  total: number
  success: number
  failed: number
  by_endpoint: Record<string, number>
  by_model?: Record<string, number>
  by_status: Record<string, number>
  by_error_code: Record<string, number>
  trend?: AdminStatsTrend
  recent_failures: Array<{
    id?: string
    time?: string
    summary?: string
    endpoint?: string
    error_code?: string
    stage?: string
    reason?: string
    conversation_id?: string
  }>
}

export interface DashboardResponse {
  status: 'ok' | 'degraded'
  healthy: boolean
  version: string
  accounts: DashboardAccountStats
  storage: {
    backend: Record<string, unknown>
    health: Record<string, unknown>
    images: Record<string, unknown>
  }
  logs: DashboardLogSummary
}
