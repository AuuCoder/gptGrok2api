import apiClient from './client'

const path = (suffix: string) => `/api/icloud/${suffix.replace(/^\//, '')}`
const requestConfig = {}

export type ICloudBridgeStatus = {
  enabled: boolean
  reachable: boolean
  base_url?: string
  status_code?: number
  error?: string
}

export type ICloudSession = {
  account_id?: string
  apple_id?: string
  account_label?: string
  saved?: boolean
  apple_account_login_saved?: boolean
  icloud_web_login_saved?: boolean
  icloud_imap_login_saved?: boolean
  apple_account_login_checked?: boolean
  apple_account_login_ok?: boolean
  icloud_web_login_checked?: boolean
  icloud_web_login_ok?: boolean
  icloud_imap_login_checked?: boolean
  icloud_imap_login_ok?: boolean
  apple_account_login_status?: string
  icloud_web_login_status?: string
  icloud_imap_login_status?: string
  [key: string]: unknown
}

export type ICloudAccount = {
  id?: string
  apple_id?: string
  label?: string
  note?: string
  status?: string
  icloud_status?: string
  [key: string]: unknown
}

export type ICloudMailbox = {
  id?: string
  email?: string
  api_url?: string
  api_token_mask?: string
  label?: string
  note?: string
  status?: string
  api_active?: boolean
  icloud_active?: boolean
  receive_count?: number
  openai_claimed?: boolean
  grok_claimed?: boolean
  account_id?: string
  account_apple_id?: string
  account_label?: string
  last_code_at?: string
  [key: string]: unknown
}

export type ICloudMessage = {
  id?: string
  mailbox_id?: string
  subject?: string
  from?: string
  body?: string
  received_at?: string
  created_at?: string
  [key: string]: unknown
}

export type ICloudPendingLogin = {
  success?: boolean
  needs_2fa?: boolean
  pending_id?: string
  apple_id?: string
  expires_at?: string
  message?: string
  session?: ICloudSession | null
  sessions?: ICloudSession[]
}

export type ICloudCodeResponse = {
  success?: boolean
  code?: string
  subject?: string
  received_at?: string
  sync_error?: string
}

export type ICloudMailboxList = {
  mailboxes?: ICloudMailbox[]
  groups?: Array<{ key?: string; title?: string; count?: number; account_id?: string }>
  pagination?: { page?: number; page_size?: number; total?: number; total_all?: number; total_pages?: number }
}

export type ICloudMailboxSyncResult = {
  account_id?: string
  apple_id?: string
  source?: string
  total?: number
  created?: number
  updated?: number
  skipped?: number
  error?: string
}

export type ICloudSchedulerEvent = {
  id?: number
  at?: string
  type?: string
  message?: string
  batch?: number
  mailbox_id?: string
  email?: string
  api_url?: string
  error?: string
}

export type ICloudScheduler = {
  running?: boolean
  owner?: string
  account_id?: string
  account_ids?: string[]
  label?: string
  note?: string
  create_channel?: string
  create_channel_label?: string
  batch_size?: number
  target_per_account?: number
  apple_account_quota?: number
  icloud_web_quota?: number
  interval_seconds?: number
  interval_minutes?: number
  round_interval_seconds?: number
  status?: string
  batch_index?: number
  success?: number
  failed?: number
  started_at?: string
  last_run_at?: string
  next_run_at?: string
  stopped_at?: string
  last_error?: string
  events?: ICloudSchedulerEvent[]
}

export type ICloudStatus = {
  success?: boolean
  is_admin?: boolean
  accounts?: ICloudAccount[]
  mailboxes?: ICloudMailbox[]
  messages?: number
  sessions?: ICloudSession[]
  icloud_sessions?: ICloudSession[]
  [key: string]: unknown
}

export const icloudApi = {
  bridgeStatus: () => apiClient.get<never, ICloudBridgeStatus>(path('bridge-status'), requestConfig),
  status: () => apiClient.get<never, ICloudStatus>(path('status'), requestConfig),
  session: () => apiClient.get<never, { sessions?: ICloudSession[]; session?: ICloudSession | null }>(path('session'), requestConfig),
  checkSession: (payload: { account_id?: string } = {}) =>
    apiClient.post<typeof payload, { message?: string; sessions?: ICloudSession[]; session?: ICloudSession | null }>(path('icloud/session/check'), payload, requestConfig),
  startLogin: (channel: 'apple' | 'icloud', payload: { apple_id: string; password: string; two_factor_method?: string }) =>
    apiClient.post<typeof payload, ICloudPendingLogin>(
      path(channel === 'apple' ? 'apple-account/login/start' : 'icloud/protocol-login/start'),
      payload,
      requestConfig,
    ),
  submit2fa: (channel: 'apple' | 'icloud', payload: { pending_id: string; code: string; phone_number?: string }) =>
    apiClient.post<typeof payload, ICloudPendingLogin>(
      path(channel === 'apple' ? 'apple-account/login/2fa' : 'icloud/protocol-login/2fa'),
      payload,
      requestConfig,
    ),
  saveImapLogin: (payload: { account_id?: string; email: string; app_password: string }) =>
    apiClient.post<typeof payload, { message?: string; sessions?: ICloudSession[] }>(path('icloud/imap-login/save'), payload, requestConfig),
  checkImapLogin: (payload: { account_id?: string } = {}) =>
    apiClient.post<typeof payload, { message?: string; sessions?: ICloudSession[] }>(path('icloud/imap-login/check'), payload, requestConfig),
  createMailboxes: (payload: { account_ids?: string[]; label?: string; note?: string; create_channel?: string }) =>
    apiClient.post<typeof payload, { message?: string; mailboxes?: ICloudMailbox[]; results?: unknown[] }>(path('icloud/mailboxes/create'), payload, requestConfig),
  syncMailboxes: (payload: { account_id?: string } = {}) =>
    apiClient.post<typeof payload, { message?: string; mailboxes?: ICloudMailbox[]; results?: ICloudMailboxSyncResult[] }>(path('icloud/mailboxes/sync'), payload, requestConfig),
  schedulerStatus: () => apiClient.get<never, { scheduler?: ICloudScheduler }>(path('icloud/scheduler/status'), requestConfig),
  startScheduler: (payload: { account_ids?: string[]; label?: string; note?: string; create_channel?: string; interval_minutes?: number; round_interval_seconds?: number }) =>
    apiClient.post<typeof payload, { message?: string; scheduler?: ICloudScheduler }>(path('icloud/scheduler/start'), payload, requestConfig),
  stopScheduler: () => apiClient.post<never, { message?: string; scheduler?: ICloudScheduler }>(path('icloud/scheduler/stop'), {}, requestConfig),
  clearSchedulerLogs: () => apiClient.post<never, { message?: string; scheduler?: ICloudScheduler }>(path('icloud/scheduler/logs/clear'), {}, requestConfig),
  syncExistingClaims: () => apiClient.post<never, { success?: boolean; projects?: Record<string, { emails?: number; updated?: number; missing?: string[] }> }>(path('claim-status/sync'), {}, requestConfig),
  listAccounts: () => apiClient.get<never, { accounts?: ICloudAccount[] }>(path('accounts'), requestConfig),
  listMailboxes: (params: { page?: number; page_size?: number; search?: string; account_id?: string } = {}) =>
    apiClient.get<never, ICloudMailboxList>(path('mailboxes'), { ...requestConfig, params }),
  syncMailbox: (id: string, after?: string) =>
    apiClient.post(path(`mailboxes/${encodeURIComponent(id)}/sync`), {}, { ...requestConfig, params: after ? { after } : undefined }),
  listMessages: (id: string) =>
    apiClient.get<never, { messages?: ICloudMessage[] }>(path(`mailboxes/${encodeURIComponent(id)}/messages`), requestConfig),
  fetchCode: (id: string, keyword = 'OpenAI') =>
    apiClient.get<never, ICloudCodeResponse>(
      path(`mailboxes/${encodeURIComponent(id)}/code`),
      { ...requestConfig, params: { keyword, peek: 1, wait_ms: 12000 } },
    ),
}
