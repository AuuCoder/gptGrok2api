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
export type GrokAccountStatusFilter = 'all' | GrokAccountStatus | 'normal' | 'limited' | 'abnormal' | 'disabled'
export type GrokAccountExportFormat = 'json' | 'txt'
export type GrokAccountSyncState =
  | 'synced'
  | 'not_synced'
  | 'not_ready'
  | 'runtime_unavailable'
  | 'sync_failed'
  | 'failed'
  | 'unknown'
export type GrokRuntimeStatus = 'active' | 'cooling' | 'rate_limited' | 'invalid' | 'expired' | 'disabled' | (string & {})
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
  runtime_status?: Partial<Record<'active' | 'cooling' | 'invalid' | 'disabled', number>>
  calls_total?: number
  quota?: Partial<Record<GrokQuotaMode, number>>
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
  tags?: string[]
  sync_state?: GrokAccountSyncState
  oauth?: GrokOAuthAccount | null
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

export type GrokAccountsRuntimeResponse = {
  summary: GrokAccountsMutationSummary
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

export type GrokAccountsBatchChatTestSummary = {
  total: number
  success: number
  limited: number
  permission: number
  failed: number
  skipped: number
}

export type GrokAccountsBatchChatTestStatus = 'queued' | 'running' | 'completed' | 'cancelled' | 'failed'

export type GrokAccountsBatchChatTestResult = {
  id: string
  status: string
  model?: string
  content?: string
  elapsed_ms?: number
  error?: string
}

export type GrokAccountsBatchChatTestJob = {
  id: string
  status: GrokAccountsBatchChatTestStatus | (string & {})
  total: number
  current: number
  current_id?: string
  cancel_requested?: boolean
  summary: GrokAccountsBatchChatTestSummary
  results: GrokAccountsBatchChatTestResult[]
  error?: string
}

export type GrokAccountsBatchChatTestResponse = {
  job: GrokAccountsBatchChatTestJob
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

  refreshRuntime(ids: string[]) {
    return apiClient.post<{ ids: string[] }, GrokAccountsRuntimeResponse>(
      `${GROK_ACCOUNTS_PATH}/runtime/refresh`,
      { ids },
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

  startBatchChatTest(payload: GrokAccountChatTestRequest) {
    return apiClient.post<GrokAccountChatTestRequest, GrokAccountsBatchChatTestResponse>(
      `${GROK_ACCOUNTS_PATH}/runtime/chat-test/batch`,
      payload,
    )
  },

  getBatchChatTestJob(jobId: string) {
    return apiClient.get<never, GrokAccountsBatchChatTestResponse>(
      `${GROK_ACCOUNTS_PATH}/runtime/chat-test/batch/${encodeURIComponent(jobId)}`,
    )
  },

  cancelBatchChatTestJob(jobId: string) {
    return apiClient.post<never, GrokAccountsBatchChatTestResponse>(
      `${GROK_ACCOUNTS_PATH}/runtime/chat-test/batch/${encodeURIComponent(jobId)}/cancel`,
    )
  },

  setRuntimeDisabled(ids: string[], disabled: boolean) {
    return apiClient.post<{ ids: string[]; disabled: boolean }, GrokAccountsDisabledResponse>(
      `${GROK_ACCOUNTS_PATH}/runtime/disabled`,
      { ids, disabled },
    )
  },

  export(format: GrokAccountExportFormat) {
    return apiClient.get<never, Blob>(`${GROK_ACCOUNTS_PATH}/export`, {
      params: { format },
      responseType: 'blob',
    })
  },
}
