import apiClient from '@/api/client'

export type GrokRuntimeStatus = {
  status: string
  size: number
  revision: number
  selection_strategy: string
}

export type GrokRuntimeStorage = {
  type: string
}

export type GrokRuntimeQuotaWindow = {
  remaining: number
  total: number
}

export type GrokRuntimeToken = {
  token: string
  pool: string
  status: string
  quota?: Partial<Record<'auto' | 'fast' | 'expert' | 'heavy' | 'console', GrokRuntimeQuotaWindow>>
  use_count?: number
  fail_count?: number
  last_used_at?: string | number | null
  tags?: string[]
}

export type GrokRuntimeTokensResponse = {
  tokens: GrokRuntimeToken[]
}

export type GrokRuntimeConfig = Record<string, unknown>

export type GrokRuntimeBatchResponse = {
  status: string
  summary?: {
    total: number
    ok: number
    fail: number
  }
}

const GROK_RUNTIME_ADMIN_PATH = '/api/grok/runtime/admin'

export const grokRuntimeApi = {
  getStatus() {
    return apiClient.get<never, GrokRuntimeStatus>(`${GROK_RUNTIME_ADMIN_PATH}/status`)
  },

  getStorage() {
    return apiClient.get<never, GrokRuntimeStorage>(`${GROK_RUNTIME_ADMIN_PATH}/storage`)
  },

  getTokens() {
    return apiClient.get<never, GrokRuntimeTokensResponse>(`${GROK_RUNTIME_ADMIN_PATH}/tokens`)
  },

  getConfig() {
    return apiClient.get<never, GrokRuntimeConfig>(`${GROK_RUNTIME_ADMIN_PATH}/config`)
  },

  updateConfig(config: GrokRuntimeConfig) {
    return apiClient.post<GrokRuntimeConfig, { status: string; message?: string }>(
      `${GROK_RUNTIME_ADMIN_PATH}/config`,
      config,
    )
  },

  refreshToken(token: string) {
    return apiClient.post<{ tokens: string[] }, GrokRuntimeBatchResponse>(
      `${GROK_RUNTIME_ADMIN_PATH}/batch/refresh`,
      { tokens: [token] },
    )
  },

  setTokenDisabled(token: string, disabled: boolean) {
    return apiClient.post<{ token: string; disabled: boolean }, { status: string; disabled: boolean }>(
      `${GROK_RUNTIME_ADMIN_PATH}/tokens/disabled`,
      { token, disabled },
    )
  },

  removeToken(token: string) {
    return apiClient.delete<never, { deleted: number }>(`${GROK_RUNTIME_ADMIN_PATH}/tokens`, {
      data: [token],
    })
  },
}
