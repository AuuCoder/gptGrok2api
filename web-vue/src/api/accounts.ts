import apiClient from './client'
import type { ProxyGroup } from './proxy'

export type AccountLane = 'fast' | 'thinking' | 'pro'
export type AccountBackendStatus = '正常' | '限流' | '异常' | '禁用'
export type AccountStatusCategory = 'normal' | 'limited' | 'abnormal' | 'disabled'
export type CheckoutLinkStatus = 'ready' | 'failed' | 'pending' | 'processing' | ''

export interface Account {
  id: string
  access_token?: string
  backend_status?: string
  status_category?: AccountStatusCategory
  status_label?: string
  email?: string
  user_id?: string
  type?: string
  source_type?: string
  proxy?: string
  group_id?: string
  checkout_link_status?: CheckoutLinkStatus
  // After final-link extraction, `checkout_url` is kept as a compatibility
  // alias. Keep the intermediate ChatGPT/hosted URLs separate so callers
  // never mistake a checkout bootstrap URL for a payment link.
  checkout_final_url?: string
  checkout_final_kind?: string
  checkout_url?: string
  checkout_hosted_url?: string
  checkout_chatgpt_url?: string
  checkout_session_id?: string
  checkout_processor_entity?: string
  checkout_channel?: string
  checkout_country?: string
  checkout_currency?: string
  checkout_ui_mode?: 'hosted' | 'custom' | string
  checkout_provider?: string
  checkout_kind?: string
  checkout_payment_method?: string
  checkout_amount_minor?: number
  checkout_is_free_trial?: boolean
  checkout_hosted_instructions_url?: string
  checkout_qr_image_url_png?: string
  checkout_qr_image_url_svg?: string
  checkout_qr_code_data?: string
  checkout_qr_expires_at?: string | number
  checkout_qr_expires_at_unix?: number
  checkout_qr_ttl_seconds?: number
  checkout_plan?: string
  checkout_session_status?: string
  checkout_payment_status?: string
  checkout_promo_campaign?: string
  checkout_promo_fallback_used?: boolean
  checkout_created_at?: string
  checkout_last_attempt_at?: string
  checkout_attempt_count?: number
  checkout_last_error?: string
  checkout_last_error_at?: string
  checkout_upstream_status?: number
  quota?: number
  image_quota_unknown?: boolean
  name: string
  status?: 'ready' | 'incomplete' | 'disabled' | 'invalid' | 'auto_disabled' | 'cooling' | 'backoff'
  status_reason?: string
  status_reason_code?:
    | 'disabled'
    | 'snlm0e_refresh_failed'
    | 'account_invalid'
    | 'pro_cooldown'
    | 'video_cooldown'
    | 'lane_backoff'
    | 'lane_degraded'
    | 'image_generation_unavailable'
    | 'image_degraded_to_fast'
    | 'parse_failure'
    | 'text_pending'
    | 'upstream_error'
    | 'image_quota_exhausted'
    | ''
  cookie: string
  snlm0e: string
  push_id: string
  push_id_set?: boolean
  enabled: boolean
  auto_disabled?: boolean
  auto_disabled_reason?: string
  auto_disabled_at?: number
  health_fail_streak?: number
  last_health_check_at?: number
  lanes: AccountLane[]
  model_ids: Record<AccountLane, string>
  failure_count: number
  success_count: number
  last_error: string
  last_error_kind?:
    | 'quota_exhausted'
    | 'media_pending'
    | 'media_generation_unavailable'
    | 'media_degraded'
    | 'lane_degraded'
    | 'parse_failure'
    | 'text_pending'
    | 'upstream_error'
    | 'auth_invalid'
    | ''
  pro_cooldown_until?: number
  pro_cooldown_seconds?: number
  pro_cooldown_local?: string
  pro_cooldown_reason?: string
  video_cooldown_until?: number
  video_cooldown_seconds?: number
  video_cooldown_local?: string
  video_cooldown_reason?: string
  snlm0e_refreshed_at?: number
  snlm0e_refresh_fail_count?: number
  snlm0e_last_refresh_error?: string
  snlm0e_next_refresh_after?: number
  daily_usage?: {
    fast: number
    thinking: number
    pro: number
    image: number
    music: number
    video: number
  }
  quota_limits?: {
    enabled: boolean
    fast: number
    thinking: number
    pro: number
    image: number
    music: number
    video: number
  }
  quota_summary?: {
    enabled: boolean
    period: string
    reset_in_seconds: number
    conversation: { used: number; limit: number; remaining: number; limited: boolean }
    pro: { used: number; limit: number; remaining: number; limited: boolean }
    image: { used: number; limit: number; remaining: number; limited: boolean }
    music: { used: number; limit: number; remaining: number; limited: boolean }
    video: { used: number; limit: number; remaining: number; limited: boolean }
  }
  lane_backoff_until?: Partial<Record<AccountLane, number>>
  lane_backoff_reason?: Partial<Record<AccountLane, string>>
  lane_backoff_kind?: Partial<Record<AccountLane, string>>
  lane_backoff_summary?: {
    active: boolean
    lanes: AccountLane[]
    max_wait_seconds: number
    summary: string
    items: Array<{
      lane: AccountLane
      wait_seconds: number
      until: number
      until_local: string
      reason: string
      kind: string
      label: string
    }>
  }
  last_used_at: number
  created_at: number
  restore_at?: number
  updated_at: number
  is_demo?: boolean
}

export interface ReverseMonitorLane {
  total: number
  enabled: number
  available: number
}

export interface ReverseMonitor {
  total_accounts: number
  lanes: Record<AccountLane, ReverseMonitorLane>
}

export interface AccountsResponse {
  accounts: Account[]
  total?: number
  all_total?: number
  page?: number
  page_size?: number
}

export interface AccountsBulkResponse {
  status: string
  success_count: number
  errors: string[]
}

export type ReverseLane = AccountLane
export type ReverseAccount = Account
export type ReverseAccountsResponse = AccountsResponse
export type ReverseAccountsBulkResponse = AccountsBulkResponse

export interface AccountGroup {
  id: string
  name: string
  proxy?: string
  proxy_group_id?: string
  enabled: boolean
  notes?: string
  account_count?: number
}

export interface AccountGroupPayload extends Partial<AccountGroup> {
  create_only?: boolean
}

export interface AccountRefreshProgress {
  total: number
  processed: number
  done: boolean
  error?: string | null
  status_counts?: Record<string, number>
  total_quota?: number
  result?: {
    refreshed?: number
    errors?: Array<{ token?: string; error?: string } | string>
    items?: BackendAccount[]
  } | null
}

type BackendAccount = Record<string, any>

type BackendAccountsResponse = {
  items?: BackendAccount[]
  total?: number
  all_total?: number
  page?: number
  page_size?: number
}

type BackendAccountMutationResponse = {
  item?: BackendAccount
  items?: BackendAccount[]
  added?: number
  skipped?: number
  refreshed?: number
  updated?: number
  removed?: number
  errors?: Array<{ token?: string; error?: string } | string>
  group_id?: string
  groups?: AccountGroup[]
}

type AccountImportPayload = Record<string, unknown>
type AccountImportOptions = {
  refresh?: boolean
  returnItems?: boolean
}
type AccountImportCleanupResponse = {
  checked?: number
  abnormal?: number
  removed?: number
}

const DEFAULT_LANES: AccountLane[] = ['fast', 'thinking', 'pro']
const EMPTY_MODEL_IDS: Record<AccountLane, string> = { fast: '', thinking: '', pro: '' }
export const ACCOUNT_BACKEND_STATUS_VALUES = ['正常', '限流', '异常', '禁用'] as const
const ACCOUNT_STATUS_CATEGORY_VALUES = ['normal', 'limited', 'abnormal', 'disabled'] as const
const STATUS_NORMAL: AccountBackendStatus = '正常'
const STATUS_DISABLED: AccountBackendStatus = '禁用'
const STATUS_LIMITED: AccountBackendStatus = '限流'
const STATUS_INVALID: AccountBackendStatus = '异常'

const accountTokenById = new Map<string, string>()

function cleanString(value: unknown): string {
  return String(value || '').trim()
}

export function normalizeAccountBackendStatus(
  value: unknown,
  fallback: AccountBackendStatus = STATUS_NORMAL,
): AccountBackendStatus {
  const raw = cleanString(value)
  return ACCOUNT_BACKEND_STATUS_VALUES.includes(raw as AccountBackendStatus) ? raw as AccountBackendStatus : fallback
}

function toTimestampSeconds(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return Math.floor(value > 10_000_000_000 ? value / 1000 : value)
  }
  const raw = cleanString(value)
  if (!raw) return 0
  const parsed = Date.parse(raw.replace(' ', 'T'))
  return Number.isNaN(parsed) ? 0 : Math.floor(parsed / 1000)
}

function maskToken(token: string): string {
  if (!token) return ''
  if (token.length <= 12) return '********'
  return `${token.slice(0, 6)}...${token.slice(-4)}`
}

function isMaskedToken(value: string): boolean {
  return value.includes('...') || /^\*+$/.test(value)
}

function displayIdForAccount(item: BackendAccount, index: number, usedIds: Set<string>): string {
  const base = (
    cleanString(item.email)
    || cleanString(item.user_id)
    || cleanString(item.account_id)
    || `account-${index + 1}`
  ).slice(0, 96)
  let candidate = base
  let suffix = 2
  while (usedIds.has(candidate)) {
    candidate = `${base}#${suffix}`
    suffix += 1
  }
  usedIds.add(candidate)
  return candidate
}

function backendStatusToFrontend(item: BackendAccount): Pick<
  Account,
  'enabled' | 'status' | 'status_reason' | 'status_reason_code' | 'last_error_kind'
> {
  const rawStatus = cleanString(item.status)
  const quota = Number(item.quota ?? 0)
  const imageQuotaUnknown = Boolean(item.image_quota_unknown)
  const lastRefreshError = cleanString(item.last_refresh_error || item.last_token_refresh_error)

  if (rawStatus === STATUS_DISABLED || rawStatus.toLowerCase() === 'disabled') {
    return {
      enabled: false,
      status: 'disabled',
      status_reason: '账号禁用',
      status_reason_code: 'disabled',
      last_error_kind: '',
    }
  }

  if (rawStatus === STATUS_INVALID || rawStatus.toLowerCase() === 'invalid') {
    return {
      enabled: true,
      status: 'invalid',
      status_reason: lastRefreshError || '账号鉴权异常',
      status_reason_code: 'account_invalid',
      last_error_kind: 'auth_invalid',
    }
  }

  const invalidCount = Number(item.invalid_count ?? 0) || 0
  if (invalidCount >= 1 && lastRefreshError && rawStatus !== STATUS_LIMITED) {
    return {
      enabled: true,
      status: 'invalid',
      status_reason: '已检测到鉴权异常，已进入统一异常处理流程。',
      status_reason_code: 'account_invalid',
      last_error_kind: 'auth_invalid',
    }
  }

  if (rawStatus === STATUS_LIMITED) {
    return {
      enabled: true,
      status: 'cooling',
      status_reason: '远程确认图片额度已用完',
      status_reason_code: 'image_quota_exhausted',
      last_error_kind: 'quota_exhausted',
    }
  }

  return {
    enabled: true,
    status: 'ready',
    status_reason: lastRefreshError || (!imageQuotaUnknown && quota <= 0 ? '本地额度待远程刷新，以请求前预检结果为准' : ''),
    status_reason_code: '',
    last_error_kind: lastRefreshError ? 'upstream_error' : '',
  }
}

function normalizeAccountStatusCategory(value: unknown): AccountStatusCategory | undefined {
  const raw = cleanString(value)
  return ACCOUNT_STATUS_CATEGORY_VALUES.includes(raw as AccountStatusCategory)
    ? raw as AccountStatusCategory
    : undefined
}

function normalizeCheckoutLinkStatus(value: unknown): CheckoutLinkStatus {
  const raw = cleanString(value).toLowerCase()
  return ['ready', 'failed', 'pending', 'processing'].includes(raw)
    ? raw as CheckoutLinkStatus
    : ''
}

function optionalNonNegativeInteger(value: unknown): number | undefined {
  if (value === null || value === undefined || value === '') return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : undefined
}

function optionalBoolean(value: unknown): boolean | undefined {
  if (value === true || String(value).toLowerCase() === 'true') return true
  if (value === false || String(value).toLowerCase() === 'false') return false
  return undefined
}

function mapBackendAccount(item: BackendAccount, index: number, usedIds: Set<string>): Account {
  const accessToken = cleanString(item.access_token || item.accessToken)
  const id = displayIdForAccount(item, index, usedIds)
  if (accessToken) accountTokenById.set(id, accessToken)

  const quota = Math.max(0, Number(item.quota ?? 0) || 0)
  const imageQuotaUnknown = Boolean(item.image_quota_unknown)
  const rawStatus = cleanString(item.status)
  const status = backendStatusToFrontend(item)
  const createdAt = toTimestampSeconds(item.created_at)
  const updatedAt = toTimestampSeconds(item.updated_at || item.last_used_at || item.created_at)
  const restoreAt = toTimestampSeconds(item.restore_at || item.quota_restore_at || item.reset_at)
  const lastRefreshError = cleanString(item.last_refresh_error || item.last_token_refresh_error)
  const type = cleanString(item.type || item.plan_type || 'free')
  const sourceType = cleanString(item.source_type || 'web')
  const email = cleanString(item.email)
  const userId = cleanString(item.user_id)
  const checkoutFinalUrl = cleanString(item.checkout_final_url)
  const checkoutFinalKind = cleanString(item.checkout_final_kind)
  const checkoutUrl = cleanString(item.checkout_url || checkoutFinalUrl)
  const checkoutStatus = normalizeCheckoutLinkStatus(item.checkout_link_status)

  return {
    id,
    access_token: accessToken,
    backend_status: rawStatus || STATUS_NORMAL,
    status_category: normalizeAccountStatusCategory(item.status_category),
    status_label: cleanString(item.status_label),
    email,
    user_id: userId,
    type,
    source_type: sourceType,
    proxy: cleanString(item.proxy),
    group_id: cleanString(item.group_id),
    checkout_link_status: checkoutStatus || (checkoutFinalUrl ? 'ready' : ''),
    checkout_final_url: checkoutFinalUrl || (checkoutFinalKind ? checkoutUrl : ''),
    checkout_final_kind: checkoutFinalKind,
    checkout_url: checkoutUrl,
    checkout_hosted_url: cleanString(item.checkout_hosted_url),
    checkout_chatgpt_url: cleanString(item.checkout_chatgpt_url),
    checkout_session_id: cleanString(item.checkout_session_id),
    checkout_processor_entity: cleanString(item.checkout_processor_entity),
    checkout_channel: cleanString(item.checkout_channel),
    checkout_country: cleanString(item.checkout_country),
    checkout_currency: cleanString(item.checkout_currency),
    checkout_ui_mode: cleanString(item.checkout_ui_mode),
    checkout_provider: cleanString(item.checkout_provider),
    checkout_kind: cleanString(item.checkout_kind),
    checkout_payment_method: cleanString(item.checkout_payment_method),
    checkout_amount_minor: optionalNonNegativeInteger(item.checkout_amount_minor),
    checkout_is_free_trial: optionalBoolean(item.checkout_is_free_trial),
    checkout_hosted_instructions_url: cleanString(item.checkout_hosted_instructions_url),
    checkout_qr_image_url_png: cleanString(item.checkout_qr_image_url_png),
    checkout_qr_image_url_svg: cleanString(item.checkout_qr_image_url_svg),
    checkout_qr_code_data: cleanString(item.checkout_qr_code_data),
    checkout_qr_expires_at: item.checkout_qr_expires_at ?? undefined,
    checkout_qr_expires_at_unix: optionalNonNegativeInteger(item.checkout_qr_expires_at_unix),
    checkout_qr_ttl_seconds: optionalNonNegativeInteger(item.checkout_qr_ttl_seconds),
    checkout_plan: cleanString(item.checkout_plan),
    checkout_session_status: cleanString(item.checkout_session_status),
    checkout_payment_status: cleanString(item.checkout_payment_status),
    checkout_promo_campaign: cleanString(item.checkout_promo_campaign),
    checkout_promo_fallback_used: Boolean(item.checkout_promo_fallback_used),
    checkout_created_at: cleanString(item.checkout_created_at),
    checkout_last_attempt_at: cleanString(item.checkout_last_attempt_at),
    checkout_attempt_count: Math.max(0, Number(item.checkout_attempt_count || 0) || 0),
    checkout_last_error: cleanString(item.checkout_last_error),
    checkout_last_error_at: cleanString(item.checkout_last_error_at),
    checkout_upstream_status: Number(item.checkout_upstream_status || 0) || undefined,
    quota,
    image_quota_unknown: imageQuotaUnknown,
    name: email || `${type} / ${sourceType}`,
    cookie: maskToken(accessToken),
    snlm0e: '',
    push_id: '',
    enabled: status.enabled,
    status: status.status,
    status_reason: status.status_reason,
    status_reason_code: status.status_reason_code,
    lanes: [...DEFAULT_LANES],
    model_ids: { ...EMPTY_MODEL_IDS },
    failure_count: Number(item.fail ?? 0) || 0,
    success_count: Number(item.success ?? 0) || 0,
    last_error: lastRefreshError,
    last_error_kind: status.last_error_kind,
    daily_usage: { fast: 0, thinking: 0, pro: 0, image: 0, music: 0, video: 0 },
    quota_limits: {
      enabled: !imageQuotaUnknown,
      fast: -1,
      thinking: -1,
      pro: -1,
      image: imageQuotaUnknown ? -1 : quota,
      music: -1,
      video: -1,
    },
    quota_summary: {
      enabled: !imageQuotaUnknown,
      period: 'current',
      reset_in_seconds: 0,
      conversation: { used: 0, limit: -1, remaining: -1, limited: false },
      pro: { used: 0, limit: -1, remaining: -1, limited: false },
      image: {
        used: 0,
        limit: imageQuotaUnknown ? -1 : quota,
        remaining: imageQuotaUnknown ? -1 : quota,
        limited: rawStatus === STATUS_LIMITED,
      },
      music: { used: 0, limit: -1, remaining: -1, limited: false },
      video: { used: 0, limit: -1, remaining: -1, limited: false },
    },
    last_used_at: toTimestampSeconds(item.last_used_at),
    created_at: createdAt,
    restore_at: restoreAt,
    updated_at: updatedAt || createdAt,
  }
}

export type AccountListParams = {
  page?: number
  page_size?: number
  keyword?: string
  status?: 'all' | 'normal' | 'limited' | 'abnormal' | 'disabled' | 'valid_checkout'
  group_id?: string
}

function mapAccountsResponse(response: BackendAccountsResponse): AccountsResponse {
  accountTokenById.clear()
  const usedIds = new Set<string>()
  const accounts = (response.items || []).map((item, index) => mapBackendAccount(item, index, usedIds))
  return {
    accounts,
    total: Number.isFinite(Number(response.total)) ? Number(response.total) : accounts.length,
    all_total: Number.isFinite(Number(response.all_total)) ? Number(response.all_total) : undefined,
    page: Number.isFinite(Number(response.page)) ? Number(response.page) : undefined,
    page_size: Number.isFinite(Number(response.page_size)) ? Number(response.page_size) : undefined,
  }
}

function resolveToken(accountIdOrToken: string): string {
  const value = cleanString(accountIdOrToken)
  return accountTokenById.get(value) || value
}

function payloadToken(payload: Partial<Account>): string {
  const mappedToken = payload.id ? accountTokenById.get(payload.id) : ''
  const candidate = cleanString(payload.access_token || payload.cookie || mappedToken)
  if (candidate && !isMaskedToken(candidate)) return candidate
  return mappedToken || ''
}

function backendStatusForEnabled(enabled: boolean | undefined): AccountBackendStatus {
  return enabled === false ? STATUS_DISABLED : STATUS_NORMAL
}

function backendStatusForPayload(payload: Partial<Account>): AccountBackendStatus {
  const raw = cleanString(payload.backend_status || payload.status)
  if (ACCOUNT_BACKEND_STATUS_VALUES.includes(raw as AccountBackendStatus)) {
    return raw as AccountBackendStatus
  }
  return backendStatusForEnabled(payload.enabled)
}

function accountFromPayload(payload: Partial<Account>) {
  const accessToken = payloadToken(payload)
  if (!accessToken) {
    throw new Error('请填写 access token')
  }
  return {
    access_token: accessToken,
    type: payload.type,
    source_type: payload.source_type,
    proxy: payload.proxy,
    group_id: payload.group_id,
    quota: payload.quota,
    status: backendStatusForPayload(payload),
  }
}

async function refreshAndPoll(accessTokens: string[]) {
  const start = await apiClient.post<{ access_tokens: string[] }, { progress_id: string }>(
    '/api/accounts/refresh',
    { access_tokens: accessTokens },
  )
  const progressId = cleanString(start.progress_id)
  if (!progressId) return { status: 'ok' }

  const deadline = Date.now() + 60_000
  while (Date.now() < deadline) {
    const progress = await apiClient.get<never, AccountRefreshProgress>(
      `/api/accounts/refresh/progress/${encodeURIComponent(progressId)}`,
    )
    const legacyProgress = progress as AccountRefreshProgress & { status?: string; finished?: boolean }
    if (progress.done || legacyProgress.status === 'done' || legacyProgress.finished) {
      if (progress.error) throw new Error(String(progress.error))
      const errors = Array.isArray(progress?.result?.errors) ? progress.result.errors : []
      if (errors.length) {
        const first = errors[0]
        const message = typeof first === 'string'
          ? first
          : [first?.token, first?.error].map(cleanString).filter(Boolean).join(': ')
        throw new Error(message || `账号刷新失败，共 ${errors.length} 个错误`)
      }
      return { status: 'ok', progress }
    }
    await new Promise((resolve) => window.setTimeout(resolve, 800))
  }
  throw new Error('刷新进度超时，请稍后重新打开列表查看结果')
}

async function refreshAndPollWithProgress(
  accountIdsOrTokens: string[],
  onProgress?: (progress: AccountRefreshProgress) => void,
  options?: { all?: boolean },
) {
  const accessTokens = Array.from(new Set(accountIdsOrTokens.map(resolveToken).filter(Boolean)))
  if (!accessTokens.length && !options?.all) {
    throw new Error('没有可刷新的 access token')
  }

  const start = await apiClient.post<{ access_tokens: string[] }, { progress_id: string }>(
    '/api/accounts/refresh',
    { access_tokens: accessTokens },
  )
  const progressId = cleanString(start.progress_id)
  if (!progressId) {
    return { status: 'ok', progress: null as AccountRefreshProgress | null }
  }

  const deadline = Date.now() + (
    options?.all ? 12 * 60 * 60 * 1000 : Math.max(90_000, accessTokens.length * 15_000)
  )
  while (Date.now() < deadline) {
    const progress = await apiClient.get<never, AccountRefreshProgress>(
      `/api/accounts/refresh/progress/${encodeURIComponent(progressId)}`,
    )
    onProgress?.(progress)
    if (progress.done || progress.error) {
      if (progress.error) throw new Error(String(progress.error))
      return { status: 'ok', progress }
    }
    await new Promise((resolve) => window.setTimeout(resolve, 900))
  }

  throw new Error('刷新进度超时，请稍后重新打开列表查看结果')
}

async function updateStatus(accountId: string, status: string) {
  const accessToken = resolveToken(accountId)
  const response = await apiClient.post<
    { access_token: string; status: string },
    BackendAccountMutationResponse
  >('/api/accounts/update', { access_token: accessToken, status })
  return {
    status: 'ok',
    account: response.item ? mapBackendAccount(response.item, 0, new Set()) : undefined,
  }
}

async function updateStatusBatch(accountIdsOrTokens: string[], status: string) {
  const accessTokens = Array.from(new Set(accountIdsOrTokens.map(resolveToken).filter(Boolean)))
  if (!accessTokens.length) {
    return { status: 'ok', success_count: 0, errors: [] as string[] }
  }
  const response = await apiClient.post<
    { access_tokens: string[]; status: string },
    BackendAccountMutationResponse
  >('/api/accounts/batch-update', { access_tokens: accessTokens, status })
  return {
    status: 'ok',
    success_count: Number(response.updated || 0),
    errors: Array.isArray(response.errors)
      ? response.errors.map((item) => (typeof item === 'string' ? item : String(item.error || item.token || ''))).filter(Boolean)
      : [],
  }
}

async function deleteAccountsByIds(accountIdsOrTokens: string[]) {
  const accessTokens = Array.from(new Set(accountIdsOrTokens.map(resolveToken).filter(Boolean)))
  if (!accessTokens.length) {
    return { status: 'ok', success_count: 0, errors: [] as string[] }
  }
  const response = await apiClient.request<unknown, BackendAccountMutationResponse>({
    method: 'DELETE',
    url: '/api/accounts',
    data: { tokens: accessTokens },
  })
  for (const accountId of accountIdsOrTokens) accountTokenById.delete(accountId)
  return {
    status: 'ok',
    success_count: Number(response.removed || 0),
    errors: Array.isArray(response.errors)
      ? response.errors.map((item) => (typeof item === 'string' ? item : String(item.error || item.token || ''))).filter(Boolean)
      : [],
  }
}

export const accountsApi = {
  list: async (params?: AccountListParams) => {
    const response = await apiClient.get<never, BackendAccountsResponse>('/api/accounts', {
      params: params || undefined,
    })
    return mapAccountsResponse(response)
  },

  listGroups: () =>
    apiClient.get<never, { groups: AccountGroup[]; proxy_groups?: ProxyGroup[] }>('/api/account-groups'),

  saveGroup: (payload: AccountGroupPayload) =>
    apiClient.post<AccountGroupPayload, { group: AccountGroup; groups: AccountGroup[]; proxy_groups?: ProxyGroup[] }>(
      '/api/account-groups',
      payload,
    ),

  deleteGroup: (id: string) =>
    apiClient.delete<never, { deleted: string; groups: AccountGroup[]; proxy_groups?: ProxyGroup[]; items?: BackendAccount[] }>(
      `/api/account-groups/${encodeURIComponent(id)}`,
    ),

  upsert: async (payload: Partial<Account>) => {
    const account = accountFromPayload(payload)
    const existingToken = payload.id ? accountTokenById.get(payload.id) : ''
    if (existingToken && existingToken === account.access_token) {
      const response = await apiClient.post<
        {
          access_token: string
          type?: string
          source_type?: string
          status?: string
          quota?: number
          proxy?: string
          group_id?: string
        },
        BackendAccountMutationResponse
      >('/api/accounts/update', {
        access_token: existingToken,
        type: account.type,
        source_type: account.source_type,
        status: account.status,
        quota: account.quota,
        proxy: account.proxy,
        group_id: account.group_id,
      })
      return {
        status: 'ok',
        account: response.item ? mapBackendAccount(response.item, 0, new Set()) : undefined,
      }
    }

    const response = await apiClient.post<
      { tokens: string[]; accounts: Array<Record<string, unknown>> },
      { items?: BackendAccount[] }
    >('/api/accounts', {
      tokens: [],
      accounts: [account],
    })
    const mapped = mapAccountsResponse({ items: response.items || [] })
    return { status: 'ok', account: mapped.accounts.find((item) => item.access_token === account.access_token) || mapped.accounts[0] }
  },

  importAccounts: async (
    accountPayloads: AccountImportPayload[],
    fallbackSourceType = 'manual',
    options: AccountImportOptions = {},
  ) => {
    const deduped = new Map<string, AccountImportPayload>()
    for (const payload of accountPayloads) {
      if (!payload || typeof payload !== 'object') continue
      const accessToken = cleanString(payload.access_token || payload.accessToken || payload.cookie)
      if (!accessToken) continue
      const nextPayload: AccountImportPayload = {
        ...payload,
        access_token: accessToken,
        source_type: cleanString(payload.source_type) || fallbackSourceType,
        status: cleanString(payload.status) || STATUS_NORMAL,
      }
      delete nextPayload.accessToken
      deduped.set(accessToken, nextPayload)
    }
    const accounts = Array.from(deduped.values())
    if (!accounts.length) {
      return { status: 'ok', added: 0, skipped: 0, refreshed: 0, errors: [] as string[] }
    }
    const response = await apiClient.post<
      {
        tokens: string[]
        accounts: Array<Record<string, unknown>>
        refresh?: boolean
        return_items?: boolean
      },
      BackendAccountMutationResponse
    >('/api/accounts', {
      tokens: [],
      accounts,
      refresh: options.refresh ?? true,
      return_items: options.returnItems ?? false,
    })
    return {
      status: 'ok',
      added: Number(response.added || 0),
      skipped: Number(response.skipped || 0),
      refreshed: Number(response.refreshed || 0),
      errors: Array.isArray(response.errors)
        ? response.errors.map((item) => (typeof item === 'string' ? item : [item.token, item.error].filter(Boolean).join(': '))).filter(Boolean)
        : [],
    }
  },

  importTokens: async (tokens: string[], sourceType: string) => {
    const accounts = Array.from(new Set(tokens.map((token) => cleanString(token)).filter(Boolean)))
      .map((accessToken) => ({
        access_token: accessToken,
        type: 'free',
        source_type: sourceType,
        status: STATUS_NORMAL,
      }))
    return accountsApi.importAccounts(accounts, sourceType)
  },

  cleanupImportedAbnormalAccounts: async (tokens: string[], remove = false) => {
    const accessTokens = Array.from(new Set(tokens.map((token) => cleanString(token)).filter(Boolean)))
    if (!accessTokens.length) {
      return { status: 'ok', checked: 0, abnormal: 0, removed: 0 }
    }
    const response = await apiClient.post<
      { access_tokens: string[]; remove: boolean },
      AccountImportCleanupResponse
    >('/api/accounts/import-cleanup', {
      access_tokens: accessTokens,
      remove,
    })
    return {
      status: 'ok',
      checked: Number(response.checked || 0),
      abnormal: Number(response.abnormal || 0),
      removed: Number(response.removed || 0),
    }
  },

  delete: async (accountId: string) => {
    await deleteAccountsByIds([accountId])
    accountTokenById.delete(accountId)
    return { status: 'ok', deleted: accountId }
  },

  refreshToken: async (accountId: string) => {
    await refreshAndPoll([resolveToken(accountId)])
    return { status: 'ok', account: undefined as unknown as Account }
  },

  extractCheckout: async (accountId: string) => {
    const payload = {
      access_token: resolveToken(accountId),
    }
    const response = await apiClient.post<
      typeof payload,
      BackendAccountMutationResponse & Record<string, unknown>
    >('/api/accounts/checkout-session', payload)
    return {
      checkout_link_status: normalizeCheckoutLinkStatus(response.checkout_link_status),
      checkout_final_url: cleanString(response.checkout_final_url),
      checkout_final_kind: cleanString(response.checkout_final_kind),
      checkout_url: cleanString(response.checkout_url),
      checkout_chatgpt_url: cleanString(response.checkout_chatgpt_url || response.chatgpt_checkout_url),
      checkout_hosted_url: cleanString(response.checkout_hosted_url || response.hosted_checkout_url),
      checkout_payment_method: cleanString(response.checkout_payment_method),
      checkout_amount_minor: optionalNonNegativeInteger(response.checkout_amount_minor),
      checkout_is_free_trial: optionalBoolean(response.checkout_is_free_trial),
      account: response.item ? mapBackendAccount(response.item, 0, new Set()) : undefined,
    }
  },

  enqueueCheckoutRetries: async (accountIds: string[]) => {
    const accessTokens = Array.from(new Set(
      accountIds.map((accountId) => resolveToken(accountId)).filter(Boolean),
    ))
    return apiClient.post<
      { access_tokens: string[] },
      { queued?: number; skipped?: number }
    >('/api/accounts/checkout-retries', { access_tokens: accessTokens })
  },

  refreshAccountsWithProgress: (
    accountIdsOrTokens: string[],
    onProgress?: (progress: AccountRefreshProgress) => void,
  ) => refreshAndPollWithProgress(accountIdsOrTokens, onProgress),

  refreshAllAccountsWithProgress: (
    onProgress?: (progress: AccountRefreshProgress) => void,
  ) => refreshAndPollWithProgress([], onProgress, { all: true }),

  exportAccounts: (accountIdsOrTokens: string[], format: 'json' | 'zip' = 'json') =>
    apiClient.post<{ access_tokens: string[]; format: 'json' | 'zip' }, Blob>('/api/accounts/export', {
      access_tokens: Array.from(new Set(accountIdsOrTokens.map(resolveToken).filter(Boolean))),
      format,
    }, {
      responseType: 'blob',
    }),

  resetAccountState: async (accountId: string) => {
    return updateStatus(accountId, STATUS_NORMAL)
  },

  bindGroup: async (accountIdsOrTokens: string[], groupId: string) => {
    const accessTokens = Array.from(new Set(accountIdsOrTokens.map(resolveToken).filter(Boolean)))
    const response = await apiClient.post<
      { access_tokens: string[]; group_id: string },
      BackendAccountMutationResponse
    >('/api/accounts/group', {
      access_tokens: accessTokens,
      group_id: groupId,
    })
    return {
      status: 'ok',
      updated: response.updated || 0,
      group_id: response.group_id || groupId,
      groups: response.groups || [],
      accounts: response.items ? mapAccountsResponse({ items: response.items }).accounts : [],
    }
  },

  enable: async (accountId: string) =>
    updateStatus(accountId, STATUS_NORMAL),

  disable: async (accountId: string) =>
    updateStatus(accountId, STATUS_DISABLED),

  bulkEnable: (accountIds: string[]) =>
    updateStatusBatch(accountIds, STATUS_NORMAL),

  bulkDisable: (accountIds: string[]) =>
    updateStatusBatch(accountIds, STATUS_DISABLED),

  bulkDelete: (accountIds: string[]) =>
    deleteAccountsByIds(accountIds),

  resolveCookie: async (_cookie: string) => ({
    status: 'unsupported',
    snlm0e: '',
    model_ids: { ...EMPTY_MODEL_IDS },
  }),
}
