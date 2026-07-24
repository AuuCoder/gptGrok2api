import apiClient from '@/api/client'
import type { GrokOAuthAccount } from '@/api/grokOAuthAccounts'

export type GrokAccountStatus =
  | 'active'
  | 'pending_sso'
  | 'submitting'
  | 'pending_submit'
  | 'submission_failed'
  | 'submission_unknown'
  | 'submission_unconfirmed'
export type GrokAccountStatusFilter =
  | 'all'
  | GrokAccountStatus
  | 'normal'
  | 'limited'
  | 'abnormal'
  | 'disabled'
  | 'refresh_failed'
  | 'probe_invalid'
  | 'probe_unknown'
  | 'oauth_unauthorized'
  | 'oauth_denied'
  | 'oauth_normal'
  | 'oauth_limited'
  | 'oauth_no_quota'
  | 'oauth_expired'
  | 'oauth_invalid'
export type GrokAccountExportFormat = 'sub2api' | 'cpa'
export type GrokAccountSyncState =
  | 'synced'
  | 'not_synced'
  | 'not_ready'
  | 'runtime_unavailable'
  | 'sync_failed'
  | 'failed'
  | 'unknown'
export type GrokRuntimeStatus = 'active' | 'cooling' | 'rate_limited' | 'invalid' | 'expired' | 'disabled' | (string & {})
export type GrokRecoveryStatus = 'pending' | 'running' | 'success' | 'failed'
export type GrokQuotaMode = 'auto' | 'fast' | 'expert' | 'heavy' | 'console'

export type GrokQuotaWindow = {
  remaining: number
  total: number
  reset_at?: number
  source?: 0 | 1 | 2
}

export type GrokQuota = Partial<Record<GrokQuotaMode, GrokQuotaWindow>>

export type GrokAccountsSummary = {
  total?: number
  active?: number
  pending?: number
  failed?: number
  synced?: number
  not_synced?: number
  runtime_total?: number
  oauth_total?: number
  oauth_linked?: number
  oauth_status?: Partial<Record<'unauthorized' | 'denied' | 'normal' | 'limited' | 'no_quota' | 'expired' | 'invalid', number>>
  runtime_status?: Partial<Record<'active' | 'cooling' | 'invalid' | 'disabled', number>>
  calls_total?: number
  quota?: Partial<Record<GrokQuotaMode, number>>
  probe?: Partial<Record<GrokAccountVerificationStatus, number>>
}

export type GrokProbeSchedulerStatus = {
  enabled: boolean
  running?: boolean
  next_run_at?: string
  interval_minutes?: number
  last_started_at?: string
  last_finished_at?: string
  last_error?: string
}

export type GrokAccount = {
  id: string
  platform: 'grok'
  email: string
  has_password: boolean
  has_sso: boolean
  source_type: string
  status: GrokAccountStatus | (string & {})
  created_at: string
  updated_at: string
  token_preview?: string
  pool?: string
  runtime_status?: GrokRuntimeStatus | null
  quota?: GrokQuota | null
  use_count?: number
  fail_count?: number
  last_used_at?: string | number | null
  refresh_status?: 'success' | 'failed' | (string & {})
  refresh_at?: string | number | null
  refresh_error?: string
  probe_status?: GrokAccountVerificationStatus | (string & {})
  probe_at?: string | number | null
  probe_quota?: GrokQuotaWindow | null
  probe_error?: string
  recovery_status?: GrokRecoveryStatus | (string & {})
  recovery_last_attempt_at?: string | number | null
  recovery_last_success_at?: string | number | null
  recovery_next_attempt_at?: string | number | null
  recovery_error?: string
  recovery_attempts?: number
  tags?: string[]
  sync_state?: GrokAccountSyncState
  oauth?: GrokOAuthAccount | null
  oauth_authorization?: {
    status?: 'pending' | 'success' | 'failed' | 'denied' | (string & {})
    attempted_at?: string | number | null
    error?: string
    attempts?: number
  }
}

export type GrokAccountsListParams = {
  page: number
  page_size: number
  keyword?: string
  status?: Exclude<GrokAccountStatusFilter, 'all'>
}

export type GrokAccountsListResponse = {
  items: GrokAccount[]
  total: number
  all_total: number
  page: number
  page_size: number
  count: number
  summary?: GrokAccountsSummary
  runtime_available?: boolean
  runtime_error?: string
  probe_scheduler?: GrokProbeSchedulerStatus
}

export type GrokProbePollingResponse = {
  probe_scheduler: GrokProbeSchedulerStatus
}

export type GrokAccountsDeleteResponse = {
  removed: number
  count: number
  upstream_deleted?: number
}

export type GrokAccountsMutationSummary = {
  total: number
  ok: number
  fail: number
}

export type GrokAccountsSyncResponse = {
  summary: GrokAccountsMutationSummary
  results?: Array<{
    id: string
    ok: boolean
    sync_state?: GrokAccountSyncState
    error?: string
  }>
  error?: string
}

export type GrokAccountsDisabledResponse = {
  disabled: boolean
  summary: GrokAccountsMutationSummary
  error?: string
}

export type GrokAccountsOAuthAuthorizeResponse = {
  summary: {
    total: number
    queued: number
    reused: number
    skipped: number
    failed: number
  }
  results?: Array<{
    id: string
    ok: boolean
    status: 'queued' | 'reused' | 'already_authorized' | 'failed'
    job_id?: string
    error?: string
  }>
}

export type GrokAccountsRuntimeResponse = {
  summary: GrokAccountsMutationSummary
  results?: Array<{
    id: string
    ok: boolean
    refresh_status?: 'success' | 'failed' | string
    error?: string
  }>
  error?: string
}

export type GrokRuntimeSnapshotResponse = {
  ok: boolean
  refreshed: boolean
  refreshing: boolean
  error?: string
}

export type GrokAccountVerificationStatus = 'valid' | 'invalid' | 'unknown'

export type GrokAccountsVerifyResponse = {
  summary: {
    total: number
    valid: number
    invalid: number
    unknown: number
  }
  results?: Array<{
    id: string
    status: GrokAccountVerificationStatus
    quota?: GrokQuota | null
    error?: string
  }>
  error?: string
}

export type GrokAccountLoginCredentials = {
  id: string
  email: string
  password: string
}

export type GrokAccountChatTestRequest = {
  prompt: string
  model?: string
}

export type GrokAccountChatTestResponse = {
  id: string
  model: string
  content: string
  elapsed_ms: number
}

const GROK_ACCOUNTS_PATH = '/api/register/grok/accounts'

export const grokAccountsApi = {
  list(params: GrokAccountsListParams) {
    return apiClient.get<never, GrokAccountsListResponse>(GROK_ACCOUNTS_PATH, { params })
  },

  loginCredentials(id: string) {
    return apiClient.get<never, GrokAccountLoginCredentials>(
      `${GROK_ACCOUNTS_PATH}/${encodeURIComponent(id)}/credentials`,
      { headers: { 'Cache-Control': 'no-store' } },
    )
  },

  remove(ids: string[], deleteUpstream = false) {
    return apiClient.delete<never, GrokAccountsDeleteResponse>(GROK_ACCOUNTS_PATH, {
      data: { ids, delete_upstream: deleteUpstream },
    })
  },

  sync(ids: string[]) {
    return apiClient.post<{ ids: string[] }, GrokAccountsSyncResponse>(
      `${GROK_ACCOUNTS_PATH}/sync`,
      { ids },
    )
  },

  authorizeOAuth(ids: string[]) {
    return apiClient.post<{ ids: string[] }, GrokAccountsOAuthAuthorizeResponse>(
      `${GROK_ACCOUNTS_PATH}/oauth/authorize`,
      { ids },
    )
  },

  refreshRuntime(ids: string[]) {
    return apiClient.post<{ ids: string[] }, GrokAccountsRuntimeResponse>(
      `${GROK_ACCOUNTS_PATH}/runtime/refresh`,
      { ids },
    )
  },

  refreshSnapshot() {
    return apiClient.post<never, GrokRuntimeSnapshotResponse>(
      `${GROK_ACCOUNTS_PATH}/runtime/snapshot`,
    )
  },

  setProbePolling(enabled: boolean) {
    return apiClient.post<{ enabled: boolean }, GrokProbePollingResponse>(
      '/api/register/grok/probe-polling',
      { enabled },
    )
  },

  verifyRuntime(ids: string[]) {
    return apiClient.post<{ ids: string[] }, GrokAccountsVerifyResponse>(
      `${GROK_ACCOUNTS_PATH}/runtime/verify`,
      { ids },
    )
  },

  chatTest(id: string, payload: GrokAccountChatTestRequest) {
    return apiClient.post<GrokAccountChatTestRequest, GrokAccountChatTestResponse>(
      `${GROK_ACCOUNTS_PATH}/${encodeURIComponent(id)}/runtime/chat-test`,
      payload,
    )
  },

  setRuntimeDisabled(ids: string[], disabled: boolean) {
    return apiClient.post<{ ids: string[]; disabled: boolean }, GrokAccountsDisabledResponse>(
      `${GROK_ACCOUNTS_PATH}/runtime/disabled`,
      { ids, disabled },
    )
  },

  export(ids: string[], format: GrokAccountExportFormat) {
    return apiClient.post<{ ids: string[]; format: GrokAccountExportFormat }, Blob>(`${GROK_ACCOUNTS_PATH}/export`, {
      ids: Array.from(new Set(ids.map((id) => String(id || '').trim()).filter(Boolean))),
      format,
    }, {
      responseType: 'blob',
    })
  },
}
