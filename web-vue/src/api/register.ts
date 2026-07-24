import apiClient from './client'

export type OutlookMailboxParseStats = {
  raw_lines?: number
  non_empty?: number
  valid?: number
  duplicates?: number
  invalid?: number
  skipped?: number
  existing_total?: number
  saved_total?: number
  issues?: Array<{
    line?: number
    reason?: string
    email?: string
  }>
  [key: string]: unknown
}

export type OutlookFailedMailbox = {
  id: string
  email: string
  reason?: string
  updated_at?: string
}

export type RegisterProvider = {
  id?: string
  provider_id?: string
  enable?: boolean
  type?: string
  label?: string
  api_base?: string
  api_key?: string
  admin_key?: string
  admin_email?: string
  admin_password?: string
  ddg_token?: string
  cf_inbox_jwt?: string
  cf_api_base?: string
  cf_api_key?: string
  cf_auth_mode?: string
  cf_create_path?: string
  cf_messages_path?: string
  default_domain?: string
  key_mode?: 'public' | 'custom' | string
  local_compose?: boolean
  project?: string
  purpose?: string
  keyword?: string
  wait_ms?: number
  use_proxy?: boolean
  email_prefix?: string
  subdomain?: string | string[]
  domain?: string[]
  cf_domain?: string[]
  random_subdomain?: boolean
  wildcard?: boolean
  expiry_time?: number
  mailboxes?: string
  mailboxes_count?: number
  mailboxes_base_count?: number
  mailboxes_alias_count?: number
  mailboxes_preview?: string[]
  mailboxes_failed?: OutlookFailedMailbox[]
  alias_enabled?: boolean
  alias_per_email?: number
  alias_prefix?: string
  alias_include_original?: boolean
  mailboxes_stats?: {
    unused?: number
    in_use?: number
    used?: number
    login_required?: number
    token_invalid?: number
    failed?: number
    available?: number
    busy?: number
    retryable?: number
    invalid?: number
    abnormal?: number
    [key: string]: number | undefined
  }
  mailboxes_parse_stats?: OutlookMailboxParseStats
  mode?: 'graph' | 'imap' | 'auto' | string
  imap_host?: string
  message_limit?: number
  [key: string]: unknown
}

export type RegisterTarget = 'openai' | 'grok'

export type GrokTurnstileProvider = 'yescaptcha' | '2captcha' | 'local' | 'custom'

export type GrokOAuthDeliveryConfig = {
  sub2api: OpenAISub2APISyncConfig
  cpa: {
    enabled: boolean
    pool_id: string
  }
}

export type GrokRegisterConfig = {
  signup_flow: 'xconsole' | 'legacy' | string
  provider: GrokTurnstileProvider | string
  api_key: string
  api_base: string
  request_timeout: number
  captcha_timeout: number
  captcha_poll_interval: number
  local_concurrency: number
  create_path: string
  result_path: string
  max_mail_retries: number
  xai_cli_oauth_enabled: boolean
  xai_cli_oauth_flow: 'device' | 'pkce_reference'
  xai_cli_pkce_reference_dir: string
  oauth_delivery: GrokOAuthDeliveryConfig
  grok2api_enabled: boolean
  grok2api_api_base: string
  grok2api_admin_key: string
  grok2api_pool: 'auto' | 'basic' | 'super' | 'heavy' | string
  grok2api_auto_nsfw: boolean
  grok2api_verify_on_import: boolean
  grok2api_timeout: number
  [key: string]: unknown
}

export type OpenAICheckoutConfig = {
  enabled: boolean
  channel: 'upi' | 'pix'
  /** Pix only: choose one of the three independently implemented flows. */
  pix_protocol?: 'enhanced' | 'reference' | 'standalone'
  country: string
  currency: string
  checkout_ui_mode: 'custom'
  threads: number
  checkout_proxy_enabled: boolean
  checkout_proxy_url: string
  promotion_proxy_enabled: boolean
  promotion_proxy_url: string
  provider_proxy_enabled: boolean
  provider_proxy_url: string
  /** Keep retrying final-link protocols with a fresh proxy allocation after a failed round. */
  continuous_retry?: boolean
  /** Legacy single-stage Checkout proxy fields accepted from stored configs. */
  residential_proxy_enabled?: boolean
  residential_proxy_url?: string
}

export type OpenAISub2APISyncConfig = {
  /** Whether newly registered OpenAI accounts should be sent to the selected Sub2API server. */
  enabled: boolean
  /** Saved Sub2API connection ID. */
  server_id: string
  /** Use an existing remote group or let the remote side create/find a named group. */
  group_mode: 'existing' | 'custom' | string
  /** Existing remote group ID when `group_mode` is `existing`. */
  group_id: string
  /** Desired remote group name when `group_mode` is `custom`. */
  group_name: string
}

export type OpenAICPASyncConfig = {
  /** Whether newly registered OpenAI accounts should be uploaded to the selected CPA server. */
  enabled: boolean
  /** Saved CPA connection ID. */
  pool_id: string
}

/** One Checkout extraction attempt reported by the registration runtime. */
export type CheckoutTask = {
  task_id?: string
  index?: number
  email?: string
  status?: string
  stage?: string
  /** Credential-free description of the exact protocol milestone. */
  progress_detail?: string
  /** Current or most recently completed extraction round. */
  attempt?: number
  /** Timestamp at which a retrying task will be picked up again. */
  next_retry_at?: string
  /** Payment-link extraction channel selected for this task. */
  channel?: 'upi' | 'pix'
  payment_link?: string
  error_short?: string
  updated_at?: string
  [key: string]: unknown
}

export type LegacyRegisterConfig = {
  target: RegisterTarget | string
  grok: GrokRegisterConfig
  checkout: OpenAICheckoutConfig
  sub2api_sync: OpenAISub2APISyncConfig
  cpa_sync: OpenAICPASyncConfig
  agent_identity_archive: {
    enabled: boolean
  }
  mail: {
    request_timeout?: number
    wait_timeout?: number
    wait_interval?: number
    user_agent?: string
    providers?: RegisterProvider[]
    [key: string]: unknown
  }
  proxy: string
  total: number
  threads: number
  mode: 'total' | 'quota' | 'available' | string
  target_quota: number
  target_available: number
  check_interval: number
  enabled: boolean
  stats?: {
    success?: number
    fail?: number
    done?: number
    running?: number
    threads?: number
    elapsed_seconds?: number
    avg_seconds?: number
    success_rate?: number
    current_quota?: number
    current_available?: number
    [key: string]: unknown
  }
  logs?: Array<{
    time: string
    text: string
    level?: string
  }>
  grok_oauth_logs?: Array<{
    time: string
    text: string
    level?: string
  }>
  checkout_logs?: Array<{
    time: string
    text: string
    level?: string
  }>
  checkout_tasks?: CheckoutTask[]
  checkout_retries_active?: boolean
  checkout_retry_job_count?: number
}

export type GptMailStatus = {
  ok?: boolean
  key_mode?: string
  api_base?: string
  source?: string
  is_active?: boolean
  daily_limit?: number | null
  used_today?: number | null
  remaining_today?: number | null
  total_limit?: number | null
  total_usage?: number | null
  remaining_total?: number | null
  reset_at?: string
  seconds_until_reset?: number | null
  checked_at?: string
  key_hint?: string
  local_compose?: boolean
  default_domain?: string
}

export const registerApi = {
  getConfig() {
    return apiClient.get<any, { register: LegacyRegisterConfig }>('/api/register')
  },
  updateConfig(payload: Partial<LegacyRegisterConfig>) {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register', payload)
  },
  startLegacy() {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register/start')
  },
  stopLegacy() {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register/stop')
  },
  stopCheckoutRetries() {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register/checkout-retries/stop')
  },
  clearCheckoutHistory() {
    return apiClient.post<any, { register: LegacyRegisterConfig; removed: number }>('/api/register/checkout-history/clear')
  },
  resetLegacy() {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register/reset')
  },
  resetOutlookPool(scope: 'all' | 'retryable' | 'invalid' | 'unused' | 'failed' = 'all') {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register/outlook-pool/reset', { scope })
  },
  retrySelectedOutlookMailboxes(providerId: string, mailboxIds: string[]) {
    return apiClient.post<any, { register: LegacyRegisterConfig }>('/api/register/outlook-pool/retry-selected', {
      provider_id: providerId,
      mailbox_ids: mailboxIds,
    })
  },
  getGptMailStatus(provider: RegisterProvider, force = true) {
    return apiClient.post<any, { status: GptMailStatus }>('/api/register/gptmail/status', { provider, force })
  },
  refreshGptMailKey(provider: RegisterProvider, force = true) {
    return apiClient.post<any, { status: GptMailStatus }>('/api/register/gptmail/refresh-key', { provider, force })
  },
  exportGrokAccounts(format: 'cpa' | 'sub2api' = 'sub2api') {
    return apiClient.get<never, Blob>('/api/register/grok/accounts/export', {
      params: { format },
      responseType: 'blob',
    })
  },
}
