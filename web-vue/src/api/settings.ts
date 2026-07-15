import apiClient from './client'
import type { ImageErrorMessages, ProxyRuntimeSettings, Settings, SettingsUpdateResponse } from '@/types/api'

export type RawSettings = Record<string, any>

export interface BackupTestResult {
  ok: boolean
  status?: number
  error?: string | null
}

export interface ImageStorageTestResult {
  ok: boolean
  status?: number
  error?: string | null
}

export interface ImageStorageSyncResult {
  uploaded: number
  skipped: number
  failed: number
}

export interface RetentionCleanupRequest {
  log_retention_days?: number
  image_retention_days?: number
}

export interface RetentionCleanupSection {
  removed: number
  kept?: number
  removed_size_bytes: number
  retention_days: number
  dry_run: boolean
}

export interface RetentionCleanupResult {
  dry_run: boolean
  logs: RetentionCleanupSection
  images: RetentionCleanupSection
  total_removed: number
  total_size_bytes: number
}

export interface AccountCleanupRequest {
  auto_remove_invalid_accounts?: boolean
  auto_remove_rate_limited_accounts?: boolean
}

export interface AccountCleanupResult {
  dry_run: boolean
  invalid: number
  rate_limited: number
  total_removed: number
  auto_remove_invalid_accounts: boolean
  auto_remove_rate_limited_accounts: boolean
}

export interface BackupState {
  running?: boolean
  last_status?: string
  last_started_at?: string
  last_finished_at?: string
  last_object_key?: string
  last_error?: string
}

export interface BackupItem {
  key: string
  name?: string
  size?: number
  size_bytes?: number
  last_modified?: string
  encrypted?: boolean
}

export interface BackupRunResult {
  key: string
  size: number
  encrypted: boolean
}

export type ThirdPartyAppsSettings = Settings['third_party_apps']

const DEFAULT_PROXY_RUNTIME_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36'

const DEFAULT_IMAGE_ERROR_MESSAGES: ImageErrorMessages = {
  fallback: '图片生成请求失败，请稍后重试。',
  quota: '图片账号额度已用完，请稍后再试或联系管理员。',
  no_account: '当前图片账号暂不可用，可能是账号池、并发或上游波动，请稍后重试。',
  local_busy: '当前没有可用的图片账号或账号并发已满，请稍后重试。',
  unsupported_model: '当前模型不支持图片生成，请检查 model 参数。',
  poll_timeout: '图片任务暂未返回结果，可能仍在排队或上游处理较慢，请重试。',
  stream_interrupted: '图片生成连接中断，可能是上游服务繁忙或网络波动，请重试。',
  connection_failed: '连接上游图片服务失败，可能是网络或代理波动，请重试。',
  connection_timeout: '连接上游图片服务超时，请稍后重试。',
  token_invalid: '图片生成账号状态异常，请稍后重试。',
  text_reply: '上游返回了文本说明，未生成图片。请调整提示词或重试。',
}

const SETTINGS_SAVE_KEYS = [
  'proxy',
  'fallback_proxy',
  'proxy_runtime',
  'base_url',
  'refresh_account_interval_minute',
  'image_retention_days',
  'log_retention_days',
  'image_poll_timeout_secs',
  'image_stream_timeout_secs',
  'image_poll_interval_secs',
  'image_poll_initial_wait_secs',
  'image_account_concurrency',
  'image_parallel_generation',
  'image_remove_conversation_after_result',
  'image_error_friendly_enabled',
  'image_error_messages',
  'image_settle_enabled',
  'image_check_before_hit_enabled',
  'image_settle_secs',
  'image_timeout_retry_secs',
  'auto_remove_invalid_accounts',
  'auto_remove_rate_limited_accounts',
  'log_levels',
  'global_system_prompt',
  'sensitive_words',
  'ai_review',
  'image_generation',
  'quota_limits',
  'runtime_capacity',
  'image_storage',
  'backup',
  'chat_completion_cache',
  'third_party_apps',
] as const

function cleanString(value: unknown): string {
  return String(value || '').trim()
}

function numberValue(value: unknown, fallback: number, min?: number) {
  const parsed = Number(value)
  const next = Number.isFinite(parsed) ? parsed : fallback
  return typeof min === 'number' ? Math.max(min, next) : next
}

function boolValue(value: unknown, fallback: boolean) {
  if (typeof value === 'boolean') return value
  if (typeof value === 'string') {
    const raw = value.trim().toLowerCase()
    if (['1', 'true', 'yes', 'on'].includes(raw)) return true
    if (['0', 'false', 'no', 'off'].includes(raw)) return false
  }
  if (value == null) return fallback
  return Boolean(value)
}

function cloneRawSettings<T>(value: T | null | undefined): RawSettings {
  return JSON.parse(JSON.stringify(value || {})) as RawSettings
}

function cloneJsonValue<T>(value: T): T {
  if (typeof value === 'undefined') return value
  return JSON.parse(JSON.stringify(value)) as T
}

export function normalizeThirdPartyApps(raw: unknown): ThirdPartyAppsSettings {
  const source = raw && typeof raw === 'object' ? raw as RawSettings : {}
  const infiniteCanvas = source.infinite_canvas && typeof source.infinite_canvas === 'object'
    ? source.infinite_canvas as RawSettings
    : {}
  return {
    infinite_canvas: {
      enabled: boolValue(infiniteCanvas.enabled, false),
      url: cleanString(infiniteCanvas.url) || 'https://canvas.best',
    },
  }
}

export function normalizeProxyRuntime(raw: unknown): ProxyRuntimeSettings {
  const source = raw && typeof raw === 'object' ? raw as RawSettings : {}
  const clearance = source.clearance && typeof source.clearance === 'object'
    ? source.clearance as RawSettings
    : {}

  const egressMode = cleanString(source.egress_mode).toLowerCase()
  const clearanceMode = cleanString(clearance.mode).toLowerCase()
  const statusCodes = Array.isArray(source.reset_session_status_codes)
    ? source.reset_session_status_codes
      .map((item) => Number(item))
      .filter((item) => Number.isInteger(item) && item >= 100 && item <= 599)
    : []

  return {
    enabled: boolValue(source.enabled, false),
    egress_mode: egressMode === 'single_proxy' ? 'single_proxy' : 'direct',
    proxy_url: cleanString(source.proxy_url),
    resource_proxy_url: cleanString(source.resource_proxy_url),
    skip_ssl_verify: boolValue(source.skip_ssl_verify, false),
    reset_session_status_codes: statusCodes.length ? Array.from(new Set(statusCodes)) : [403],
    clearance: {
      enabled: boolValue(clearance.enabled, false),
      mode: ['manual', 'flaresolverr'].includes(clearanceMode) ? clearanceMode as ProxyRuntimeSettings['clearance']['mode'] : 'none',
      cf_cookies: cleanString(clearance.cf_cookies),
      cf_clearance: cleanString(clearance.cf_clearance),
      has_cf_cookies: boolValue(clearance.has_cf_cookies, false),
      has_cf_clearance: boolValue(clearance.has_cf_clearance, false),
      user_agent: cleanString(clearance.user_agent) || DEFAULT_PROXY_RUNTIME_USER_AGENT,
      browser: cleanString(clearance.browser) || 'chrome',
      flaresolverr_url: cleanString(clearance.flaresolverr_url),
      timeout_sec: numberValue(clearance.timeout_sec, 60, 1),
      refresh_interval: numberValue(clearance.refresh_interval, 3600, 60),
      warm_up_on_start: boolValue(clearance.warm_up_on_start, false),
    },
  }
}

function normalizeImageErrorMessages(raw: unknown): ImageErrorMessages {
  const source = raw && typeof raw === 'object' ? raw as RawSettings : {}
  return Object.fromEntries(
    Object.entries(DEFAULT_IMAGE_ERROR_MESSAGES).map(([key, fallback]) => [
      key,
      cleanString(source[key]) || fallback,
    ]),
  ) as ImageErrorMessages
}

export function normalizeSettings(raw: RawSettings | null | undefined): Settings {
  const source = { ...(raw || {}) }
  const basic = source.basic && typeof source.basic === 'object' ? source.basic : {}
  const imageStorage = source.image_storage && typeof source.image_storage === 'object' ? source.image_storage : {}
  const aiReview = source.ai_review && typeof source.ai_review === 'object' ? source.ai_review : {}
  const backup = source.backup && typeof source.backup === 'object' ? source.backup : {}
  const backupInclude = backup.include && typeof backup.include === 'object' ? backup.include : {}
  const thirdPartyApps = normalizeThirdPartyApps(source.third_party_apps)
  const proxyRuntime = normalizeProxyRuntime(source.proxy_runtime)

  const normalized = {
    ...source,
    proxy: cleanString(source.proxy ?? basic.proxy),
    fallback_proxy: cleanString(source.fallback_proxy),
    proxy_runtime: proxyRuntime,
    base_url: cleanString(source.base_url ?? basic.base_url),
    refresh_account_interval_minute: numberValue(source.refresh_account_interval_minute, 5, 1),
    image_retention_days: numberValue(source.image_retention_days ?? basic.image_expire_hours, 15, 1),
    log_retention_days: numberValue(source.log_retention_days, 30, 1),
    image_poll_timeout_secs: numberValue(source.image_poll_timeout_secs, 120, 1),
    image_stream_timeout_secs: numberValue(source.image_stream_timeout_secs, 300, 1),
    image_poll_interval_secs: numberValue(source.image_poll_interval_secs, 10, 0.5),
    image_poll_initial_wait_secs: numberValue(source.image_poll_initial_wait_secs, 10, 0),
    image_account_concurrency: numberValue(source.image_account_concurrency, 3, 1),
    image_parallel_generation: boolValue(source.image_parallel_generation, true),
    image_remove_conversation_after_result: boolValue(source.image_remove_conversation_after_result, false),
    image_error_friendly_enabled: boolValue(source.image_error_friendly_enabled, false),
    image_error_messages: normalizeImageErrorMessages(source.image_error_messages),
    image_settle_enabled: boolValue(source.image_settle_enabled, true),
    image_check_before_hit_enabled: boolValue(source.image_check_before_hit_enabled, true),
    image_settle_secs: numberValue(source.image_settle_secs, 5, 0.5),
    image_timeout_retry_secs: numberValue(source.image_timeout_retry_secs, 30, 1),
    auto_remove_invalid_accounts: boolValue(source.auto_remove_invalid_accounts, false),
    auto_remove_rate_limited_accounts: boolValue(source.auto_remove_rate_limited_accounts, false),
    log_levels: Array.isArray(source.log_levels)
      ? source.log_levels.map((item) => cleanString(item).toLowerCase()).filter((item) => ['debug', 'info', 'warning', 'error'].includes(item))
      : [],
    global_system_prompt: cleanString(source.global_system_prompt),
    sensitive_words: Array.isArray(source.sensitive_words)
      ? source.sensitive_words.map((item) => cleanString(item)).filter(Boolean)
      : [],
    ai_review: {
      enabled: boolValue(aiReview.enabled, false),
      base_url: cleanString(aiReview.base_url),
      api_key: cleanString(aiReview.api_key),
      model: cleanString(aiReview.model),
      prompt: cleanString(aiReview.prompt),
    },
    basic: {
      ...basic,
      api_key: cleanString(basic.api_key),
      base_url: cleanString(source.base_url ?? basic.base_url),
      proxy: cleanString(source.proxy ?? basic.proxy),
      image_expire_hours: numberValue(source.image_retention_days ?? basic.image_expire_hours, 15, 1),
    },
    image_generation: {
      enabled: boolValue(source.image_generation?.enabled, true),
      supported_models: Array.isArray(source.image_generation?.supported_models) ? source.image_generation.supported_models : [],
      model_options: Array.isArray(source.image_generation?.model_options) ? source.image_generation.model_options : [],
      block_rich_output_on_base_chat_models: boolValue(source.image_generation?.block_rich_output_on_base_chat_models, true),
      output_format: source.image_generation?.output_format === 'base64' ? 'base64' : 'url',
      nanobanana_lane: source.image_generation?.nanobanana_lane || 'fast',
      nanobanana_lane_order: Array.isArray(source.image_generation?.nanobanana_lane_order)
        ? source.image_generation.nanobanana_lane_order
        : ['fast'],
    },
    quota_limits: {
      enabled: boolValue(source.quota_limits?.enabled, true),
      fast_daily_limit: numberValue(source.quota_limits?.fast_daily_limit, -1),
      thinking_daily_limit: numberValue(source.quota_limits?.thinking_daily_limit, -1),
      pro_daily_limit: numberValue(source.quota_limits?.pro_daily_limit, -1),
      image_daily_limit: numberValue(source.quota_limits?.image_daily_limit, -1),
      music_daily_limit: numberValue(source.quota_limits?.music_daily_limit, -1),
      video_daily_limit: numberValue(source.quota_limits?.video_daily_limit, -1),
    },
    runtime_capacity: {
      uvicorn_workers: numberValue(source.runtime_capacity?.uvicorn_workers, 4, 1),
      text_concurrency_limit: numberValue(source.runtime_capacity?.text_concurrency_limit, 120, 1),
      image_concurrency_limit: numberValue(source.runtime_capacity?.image_concurrency_limit, 24, 1),
      request_queue_timeout_seconds: numberValue(source.runtime_capacity?.request_queue_timeout_seconds, 2, 0.1),
    },
    image_storage: {
      enabled: boolValue(imageStorage.enabled, false),
      mode: ['webdav', 'both'].includes(cleanString(imageStorage.mode)) ? cleanString(imageStorage.mode) : 'local',
      webdav_url: cleanString(imageStorage.webdav_url),
      webdav_username: cleanString(imageStorage.webdav_username),
      webdav_password: cleanString(imageStorage.webdav_password),
      webdav_root_path: cleanString(imageStorage.webdav_root_path) || 'chatgpt2api/images',
      public_base_url: cleanString(imageStorage.public_base_url),
    },
    backup: {
      enabled: boolValue(backup.enabled, false),
      provider: 'cloudflare_r2',
      account_id: cleanString(backup.account_id),
      access_key_id: cleanString(backup.access_key_id),
      secret_access_key: cleanString(backup.secret_access_key),
      bucket: cleanString(backup.bucket),
      prefix: cleanString(backup.prefix) || 'backups',
      interval_minutes: numberValue(backup.interval_minutes, 1440, 1),
      rotation_keep: numberValue(backup.rotation_keep, 10, 0),
      encrypt: boolValue(backup.encrypt, false),
      passphrase: cleanString(backup.passphrase),
      include: {
        config: boolValue(backupInclude.config, true),
        register: boolValue(backupInclude.register, true),
        cpa: boolValue(backupInclude.cpa, true),
        sub2api: boolValue(backupInclude.sub2api, true),
        logs: boolValue(backupInclude.logs, true),
        dashboard_metrics: boolValue(backupInclude.dashboard_metrics, true),
        image_tasks: boolValue(backupInclude.image_tasks, true),
        accounts_snapshot: boolValue(backupInclude.accounts_snapshot, true),
        auth_keys_snapshot: boolValue(backupInclude.auth_keys_snapshot, true),
        images: boolValue(backupInclude.images, false),
      },
    },
    third_party_apps: {
      infinite_canvas: {
        enabled: thirdPartyApps.infinite_canvas.enabled,
        url: thirdPartyApps.infinite_canvas.url,
      },
    },
    proxy_profiles: Array.isArray(source.proxy_profiles) ? source.proxy_profiles : [],
  } as Settings

  return normalized
}

export function prepareSettingsForEdit(raw: RawSettings | Settings | null | undefined): Settings {
  return normalizeSettings(cloneRawSettings(raw))
}

function toBackendSettings(settings: Settings): RawSettings {
  const normalized = prepareSettingsForEdit(settings)
  const payload: RawSettings = {
    proxy: cleanString(normalized.proxy),
    fallback_proxy: cleanString(normalized.fallback_proxy),
    proxy_runtime: normalizeProxyRuntime(normalized.proxy_runtime),
    base_url: cleanString(normalized.base_url),
    refresh_account_interval_minute: numberValue(normalized.refresh_account_interval_minute, 5, 1),
    image_poll_timeout_secs: numberValue(normalized.image_poll_timeout_secs, 120, 1),
    image_stream_timeout_secs: numberValue(normalized.image_stream_timeout_secs, 300, 1),
    image_poll_interval_secs: numberValue(normalized.image_poll_interval_secs, 10, 0.5),
    image_poll_initial_wait_secs: numberValue(normalized.image_poll_initial_wait_secs, 10, 0),
    image_account_concurrency: numberValue(normalized.image_account_concurrency, 3, 1),
    image_parallel_generation: boolValue(normalized.image_parallel_generation, true),
    image_remove_conversation_after_result: boolValue(normalized.image_remove_conversation_after_result, false),
    image_error_friendly_enabled: boolValue(normalized.image_error_friendly_enabled, false),
    image_error_messages: cloneRawSettings(normalized.image_error_messages),
    image_settle_enabled: boolValue(normalized.image_settle_enabled, true),
    image_check_before_hit_enabled: boolValue(normalized.image_check_before_hit_enabled, true),
    image_settle_secs: numberValue(normalized.image_settle_secs, 5, 0.5),
    image_timeout_retry_secs: numberValue(normalized.image_timeout_retry_secs, 30, 1),
    auto_remove_invalid_accounts: boolValue(normalized.auto_remove_invalid_accounts, false),
    auto_remove_rate_limited_accounts: boolValue(normalized.auto_remove_rate_limited_accounts, false),
    log_levels: Array.isArray(normalized.log_levels) ? [...normalized.log_levels] : [],
    global_system_prompt: cleanString(normalized.global_system_prompt),
    sensitive_words: Array.isArray(normalized.sensitive_words) ? [...normalized.sensitive_words] : [],
    ai_review: cloneRawSettings(normalized.ai_review),
    image_generation: cloneRawSettings(normalized.image_generation),
    quota_limits: cloneRawSettings(normalized.quota_limits),
    runtime_capacity: cloneRawSettings(normalized.runtime_capacity),
    image_storage: cloneRawSettings(normalized.image_storage),
    backup: cloneRawSettings(normalized.backup),
    chat_completion_cache: cloneRawSettings(normalized.chat_completion_cache),
    third_party_apps: cloneRawSettings(normalized.third_party_apps),
  }
  payload.image_retention_days = numberValue(
    normalized.image_retention_days,
    15,
    1,
  )
  payload.log_retention_days = numberValue(
    normalized.log_retention_days,
    30,
    1,
  )
  return payload
}

export function prepareSettingsForSave(settings: Settings): RawSettings {
  return toBackendSettings(settings)
}

export function prepareSettingsPatch(settings: Settings, baseline?: Settings | null): RawSettings {
  const next = toBackendSettings(settings)
  if (!baseline) return next
  const previous = toBackendSettings(baseline)
  return Object.fromEntries(
    SETTINGS_SAVE_KEYS
      .filter((key) => JSON.stringify(next[key]) !== JSON.stringify(previous[key]))
      .map((key) => [key, cloneJsonValue(next[key])]),
  )
}

export const settingsApi = {
  async get() {
    const response = await apiClient.get<never, { config: RawSettings }>('/api/settings')
    return normalizeSettings(response.config)
  },
  async getThirdPartyApps() {
    const response = await apiClient.get<never, { third_party_apps: RawSettings }>('/api/third-party-apps')
    return normalizeThirdPartyApps(response.third_party_apps)
  },

  async update(settings: Settings): Promise<SettingsUpdateResponse> {
    const response = await apiClient.post<RawSettings, { config: RawSettings }>('/api/settings', toBackendSettings(settings))
    return {
      status: 'ok',
      message: 'saved',
      config: normalizeSettings(response.config),
    }
  },

  async updatePartial(payload: RawSettings): Promise<SettingsUpdateResponse> {
    const response = await apiClient.post<RawSettings, { config: RawSettings }>('/api/settings', cloneRawSettings(payload))
    return {
      status: 'ok',
      message: 'saved',
      config: normalizeSettings(response.config),
    }
  },

  testBackup: () =>
    apiClient.post<Record<string, never>, { result: BackupTestResult }>('/api/backup/test', {}),

  listBackups: () =>
    apiClient.get<never, { items: BackupItem[]; state: BackupState; settings: RawSettings }>('/api/backups'),

  runBackup: () =>
    apiClient.post<Record<string, never>, { result: BackupRunResult }>('/api/backups/run', {}),

  deleteBackup: (key: string) =>
    apiClient.post<{ key: string }, { ok: boolean }>('/api/backups/delete', { key }),

  testImageStorage: () =>
    apiClient.post<Record<string, never>, { result: ImageStorageTestResult }>('/api/image-storage/test', {}),

  syncImageStorage: () =>
    apiClient.post<Record<string, never>, { result: ImageStorageSyncResult }>('/api/image-storage/sync', {}),

  previewRetentionCleanup: (payload: RetentionCleanupRequest = {}) =>
    apiClient.post<RetentionCleanupRequest, RetentionCleanupResult>('/api/settings/retention-cleanup/preview', payload),

  runRetentionCleanup: (payload: RetentionCleanupRequest = {}) =>
    apiClient.post<RetentionCleanupRequest, RetentionCleanupResult>('/api/settings/retention-cleanup/run', payload),

  previewAccountCleanup: (payload: AccountCleanupRequest = {}) =>
    apiClient.post<AccountCleanupRequest, AccountCleanupResult>('/api/settings/account-cleanup/preview', payload),

  runAccountCleanup: (payload: AccountCleanupRequest = {}) =>
    apiClient.post<AccountCleanupRequest, AccountCleanupResult>('/api/settings/account-cleanup/run', payload),
}
