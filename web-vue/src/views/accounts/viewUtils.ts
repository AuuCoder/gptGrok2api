import type { Account, AccountGroup, AccountLane } from '@/api/accounts'
import type { ProxyGroup } from '@/api/proxy'
import { parseProxyReference, proxyReferenceLabel } from '@/api/proxy'
import { PILL_TONE_CLASS } from '@/lib/pillTones'

export type QuotaKey = 'fast' | 'thinking' | 'pro' | 'image' | 'music' | 'video'
export type AccountStatusFilter = 'all' | 'normal' | 'limited' | 'abnormal' | 'disabled'

export type QuotaLine = {
  used: number
  limit: number
  remaining: number
  limited: boolean
}

type GroupLevel = 'ok' | 'warn' | 'error'
type GroupStatus = 'ok' | 'cooldown' | 'local_full' | 'upstream_hint'

type GroupState = {
  id: 'pro' | 'image' | 'music' | 'video'
  title: string
  quotaKey: QuotaKey
  status: GroupStatus
  level: GroupLevel
  detail: string
}

type GroupEvalContext = {
  item: Account
  line: QuotaLine
  resetHint: string
}

type GroupDefinition = {
  id: GroupState['id']
  title: string
  quotaKey: QuotaKey
  evaluate: (ctx: GroupEvalContext) => GroupState
}

export const quotaOrder: QuotaKey[] = ['fast', 'thinking', 'pro', 'image', 'music', 'video']

const laneOrder: AccountLane[] = ['fast', 'thinking', 'pro']

export type AccountGroupRow = AccountGroup & {
  raw: AccountGroup
  account_count: number
  proxy_label: string
}

export type AccountProgressMetricItem = {
  key: string
  label: string
  value: string | number
}

const IMAGE_UNAVAILABLE_HINTS = [
  '不能创建图片',
  '无法生成更多图像',
  '今天无法为您生成更多图像',
  '请明天再来',
  "can't create any",
  'cannot create any',
  "can't seem to create any",
  "image creation isn't available",
  'image creation may not be available',
  'unable to generate more images',
  "can't generate more images",
  'generate more images today',
]

const ACCOUNT_STATUS_CATEGORY_VALUES = ['normal', 'limited', 'abnormal', 'disabled'] as const
const ACCOUNT_STATUS_LABELS: Record<Exclude<AccountStatusFilter, 'all'>, string> = {
  normal: '正常',
  limited: '限流',
  abnormal: '异常',
  disabled: '禁用',
}

function cleanString(value: unknown): string {
  return String(value || '').trim()
}

function accountStatusCategoryValue(item: Account): Exclude<AccountStatusFilter, 'all'> | '' {
  const raw = cleanString(item.status_category)
  return ACCOUNT_STATUS_CATEGORY_VALUES.includes(raw as Exclude<AccountStatusFilter, 'all'>)
    ? raw as Exclude<AccountStatusFilter, 'all'>
    : ''
}

function signatureValue(value: unknown): string {
  return cleanString(value).replaceAll('|', '/')
}

export function boundedSignatureText(value: unknown, limit = 160): string {
  const text = signatureValue(value)
  if (text.length <= limit) return text
  return `${text.length}:${text.slice(0, limit)}:${text.slice(-24)}`
}

export function accountRowSignature(item: Account): string {
  return [
    item.id,
    rowClass(item),
    accountTokenPreview(item),
    accountSourceText(item),
    statusText(item),
    statusClass(item),
    boundedSignatureText(statusRawError(item)),
    accountPrimaryText(item),
    accountSecondaryText(item),
    accountCreatedText(item),
    accountRestoreText(item),
    accountQuotaText(item),
    boundedSignatureText(item.access_token, 48),
    boundedSignatureText(item.cookie, 48),
    item.type,
    item.source_type,
    item.group_id,
    item.proxy,
    item.checkout_link_status,
    item.checkout_channel,
    item.checkout_final_kind,
    boundedSignatureText(checkoutFinalLinkUrl(item), 64),
    item.checkout_payment_method,
    item.checkout_amount_minor ?? '',
    item.checkout_is_free_trial ? 1 : 0,
    boundedSignatureText(item.checkout_last_error),
    item.checkout_attempt_count || 0,
    item.backend_status,
    item.image_quota_unknown ? 1 : 0,
    item.success_count || 0,
    item.failure_count || 0,
    item.enabled ? 1 : 0,
    item.is_demo ? 1 : 0,
  ].map(signatureValue).join('|')
}

function includesHint(input: unknown, hints: string[]): boolean {
  const text = String(input || '').toLowerCase()
  return Boolean(text) && hints.some((hint) => text.includes(hint.toLowerCase()))
}

function normalizeLimit(value: unknown): number {
  const n = Number(value)
  return Number.isFinite(n) ? Math.trunc(n) : -1
}

function normalizeUsed(value: unknown): number {
  const n = Number(value)
  return Number.isFinite(n) ? Math.max(0, Math.trunc(n)) : 0
}

function buildQuotaLine(used: number, limit: number): QuotaLine {
  if (limit < 0) {
    return { used, limit: -1, remaining: -1, limited: false }
  }
  const safeLimit = Math.max(0, limit)
  return {
    used,
    limit: safeLimit,
    remaining: Math.max(0, safeLimit - used),
    limited: true,
  }
}

function formatDateTime(timestampSeconds: number): string {
  const date = new Date(timestampSeconds * 1000)
  if (Number.isNaN(date.getTime())) return '-'
  const yyyy = date.getFullYear()
  const mm = String(date.getMonth() + 1).padStart(2, '0')
  const dd = String(date.getDate()).padStart(2, '0')
  const hh = String(date.getHours()).padStart(2, '0')
  const mi = String(date.getMinutes()).padStart(2, '0')
  return `${yyyy}-${mm}-${dd} ${hh}:${mi}`
}

export function formatAccountDate(timestampSeconds?: number): string {
  const value = Number(timestampSeconds || 0)
  if (!Number.isFinite(value) || value <= 0) return '-'
  return formatDateTime(value)
}

export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  if (hours > 0) return `${hours}h${minutes}m`
  return `${Math.max(1, minutes)}m`
}

function quotaResetHint(item: Account): string {
  const resetInSeconds = Number(item.quota_summary?.reset_in_seconds || 0)
  return resetInSeconds > 0 ? `约 ${formatDuration(resetInSeconds)} 后重置` : ''
}

export function quotaLine(item: Account, key: QuotaKey): QuotaLine {
  const usage = item.daily_usage || { fast: 0, thinking: 0, pro: 0, image: 0, music: 0, video: 0 }
  const limits = item.quota_limits || { enabled: true, fast: -1, thinking: -1, pro: -1, image: -1, music: -1, video: -1 }
  return buildQuotaLine(normalizeUsed(usage[key]), normalizeLimit(limits[key]))
}

const GROUPS: GroupDefinition[] = [
  {
    id: 'pro',
    title: 'Pro',
    quotaKey: 'pro',
    evaluate: ({ item, line, resetHint }) => {
      const cooldownSeconds = Number(item.pro_cooldown_seconds || 0)
      if (cooldownSeconds > 0) {
        return {
          id: 'pro',
          title: 'Pro',
          quotaKey: 'pro',
          status: 'cooldown',
          level: 'warn',
          detail: `Pro：冷却 ${formatDuration(cooldownSeconds)}（到 ${formatDateTime(Number(item.pro_cooldown_until || 0))}）`,
        }
      }
      if (line.limited && line.remaining <= 0) {
        return {
          id: 'pro',
          title: 'Pro',
          quotaKey: 'pro',
          status: 'local_full',
          level: 'error',
          detail: resetHint ? `Pro：本地配额已满，${resetHint}` : 'Pro：本地配额已满',
        }
      }
      return {
        id: 'pro',
        title: 'Pro',
        quotaKey: 'pro',
        status: 'ok',
        level: 'ok',
        detail: 'Pro：正常',
      }
    },
  },
  {
    id: 'image',
    title: '图片',
    quotaKey: 'image',
    evaluate: ({ item, line, resetHint }) => {
      if (line.limited && line.remaining <= 0) {
        return {
          id: 'image',
          title: '图片',
          quotaKey: 'image',
          status: 'local_full',
          level: 'error',
          detail: resetHint ? `图片：本地配额已满，${resetHint}` : '图片：本地配额已满',
        }
      }

      if (item.status_reason_code === 'image_degraded_to_fast' || String(item.last_error_kind || '') === 'media_degraded') {
        return {
          id: 'image',
          title: '图片',
          quotaKey: 'image',
          status: 'upstream_hint',
          level: 'warn',
          detail: '图片：本次请求被降级到 Fast，建议检查登录状态或上游能力',
        }
      }

      if (
        item.status_reason_code === 'image_generation_unavailable' ||
        String(item.last_error_kind || '') === 'media_generation_unavailable' ||
        includesHint(item.status_reason, IMAGE_UNAVAILABLE_HINTS) ||
        includesHint(item.last_error, IMAGE_UNAVAILABLE_HINTS)
      ) {
        const detail = String(item.status_reason || item.last_error || '').trim()
        return {
          id: 'image',
          title: '图片',
          quotaKey: 'image',
          status: 'upstream_hint',
          level: 'warn',
          detail: detail ? `图片：${detail}` : '图片：上游暂时不可用',
        }
      }

      return {
        id: 'image',
        title: '图片',
        quotaKey: 'image',
        status: 'ok',
        level: 'ok',
        detail: '图片：正常',
      }
    },
  },
  {
    id: 'video',
    title: '视频',
    quotaKey: 'video',
    evaluate: ({ item, line, resetHint }) => {
      const cooldownSeconds = Number(item.video_cooldown_seconds || 0)
      if (cooldownSeconds > 0) {
        return {
          id: 'video',
          title: '视频',
          quotaKey: 'video',
          status: 'cooldown',
          level: 'warn',
          detail: `视频：冷却 ${formatDuration(cooldownSeconds)}（到 ${formatDateTime(Number(item.video_cooldown_until || 0))}）`,
        }
      }
      if (line.limited && line.remaining <= 0) {
        return {
          id: 'video',
          title: '视频',
          quotaKey: 'video',
          status: 'local_full',
          level: 'error',
          detail: resetHint ? `视频：本地配额已满，${resetHint}` : '视频：本地配额已满',
        }
      }
      return {
        id: 'video',
        title: '视频',
        quotaKey: 'video',
        status: 'ok',
        level: 'ok',
        detail: '视频：正常',
      }
    },
  },
  {
    id: 'music',
    title: '音乐',
    quotaKey: 'music',
    evaluate: ({ line, resetHint }) => {
      if (line.limited && line.remaining <= 0) {
        return {
          id: 'music',
          title: '音乐',
          quotaKey: 'music',
          status: 'local_full',
          level: 'error',
          detail: resetHint ? `音乐：本地配额已满，${resetHint}` : '音乐：本地配额已满',
        }
      }
      return {
        id: 'music',
        title: '音乐',
        quotaKey: 'music',
        status: 'ok',
        level: 'ok',
        detail: '音乐：正常',
      }
    },
  },
]

function getGroupStates(item: Account): GroupState[] {
  const resetHint = quotaResetHint(item)
  return GROUPS.map((group) => group.evaluate({ item, line: quotaLine(item, group.quotaKey), resetHint }))
}

function laneBackoffDetailLines(item: Account): string[] {
  const summary = item.lane_backoff_summary
  if (!summary?.active || !Array.isArray(summary.items)) return []
  return summary.items.map((entry) => {
    const lane = String(entry.lane || '').trim()
    const waitSeconds = Number(entry.wait_seconds || 0)
    const untilLocal = String(entry.until_local || '').trim()
    const reason = String(entry.reason || '').trim()
    const base = lane
      ? `${lane}：限流 ${formatDuration(waitSeconds)}`
      : `限流 ${formatDuration(waitSeconds)}`
    const withUntil = untilLocal ? `${base}（到 ${untilLocal}）` : base
    return reason ? `${withUntil}；原因：${reason}` : withUntil
  })
}

function getStateByQuotaKey(states: GroupState[], key: QuotaKey): GroupState | null {
  return states.find((state) => state.quotaKey === key) || null
}

export function quotaIssueDetailLines(item: Account): string[] {
  return [
    ...laneBackoffDetailLines(item),
    ...getGroupStates(item)
    .filter((state) => state.status !== 'ok')
    .map((state) => state.detail),
  ]
}

export function statusText(item: Account): string {
  const category = accountStatusCategoryValue(item)
  if (category) return ACCOUNT_STATUS_LABELS[category]

  const backendStatus = String(item.backend_status || '').trim()
  const status = String(item.status || '').trim().toLowerCase()
  const reasonCode = String(item.status_reason_code || '').toLowerCase()
  const errorKind = String(item.last_error_kind || '').toLowerCase()

  if (!item.enabled || status === 'disabled' || backendStatus === '禁用') return '禁用'
  if (
    backendStatus === '异常' ||
    status === 'abnormal' ||
    status === 'invalid' ||
    status === 'incomplete' ||
    reasonCode === 'snlm0e_refresh_failed' ||
    reasonCode === 'account_invalid' ||
    reasonCode === 'parse_failure' ||
    reasonCode === 'upstream_error' ||
    errorKind === 'auth_invalid' ||
    errorKind === 'parse_failure' ||
    errorKind === 'upstream_error'
  ) return '异常'
  if (
    backendStatus === '限流' ||
    status === 'limited' ||
    status === 'rate_limited' ||
    status === 'cooling' ||
    status === 'backoff' ||
    reasonCode === 'lane_backoff' ||
    reasonCode === 'pro_cooldown' ||
    reasonCode === 'video_cooldown' ||
    reasonCode === 'image_generation_unavailable' ||
    reasonCode === 'image_degraded_to_fast' ||
    reasonCode === 'lane_degraded' ||
    reasonCode === 'text_pending' ||
    errorKind === 'quota_exhausted' ||
    errorKind === 'media_pending' ||
    errorKind === 'media_generation_unavailable' ||
    errorKind === 'media_degraded' ||
    errorKind === 'lane_degraded' ||
    errorKind === 'text_pending'
  ) return '限流'
  return '正常'
}

export function statusCategory(item: Account): Exclude<AccountStatusFilter, 'all'> {
  const category = accountStatusCategoryValue(item)
  if (category) return category

  const text = statusText(item)
  if (text === '禁用') return 'disabled'
  if (text === '正常') return 'normal'
  if (text === '限流') return 'limited'
  return 'abnormal'
}

export function statusClass(item: Account): string {
  const category = statusCategory(item)
  if (category === 'normal') return PILL_TONE_CLASS.success
  if (category === 'limited') return PILL_TONE_CLASS.warning
  if (category === 'abnormal') return PILL_TONE_CLASS.danger
  if (category === 'disabled') return PILL_TONE_CLASS.neutral
  return PILL_TONE_CLASS.neutral
}

export function statusReason(item: Account): string {
  const explicitReason = String(item.status_reason || '').trim()
  if (explicitReason) return explicitReason

  if (String(item.last_error_kind || '').toLowerCase() === 'auth_invalid') {
    return '登录态失效'
  }

  const issueLines = quotaIssueDetailLines(item)
  if (issueLines.length > 0) return issueLines.join('；')

  const lastError = String(item.last_error || '').trim()
  if (lastError) return lastError
  if (!item.enabled || item.status === 'disabled') return '账号禁用'
  if (item.status === 'incomplete') return '配置不完整，请检查 access token、账号类型或代理'
  if (item.status === 'invalid') return '账号鉴权异常'
  return '账号正常可用'
}

export function statusRawError(item: Account): string {
  const raw = String(item.last_error || '').trim()
  if (!raw) return ''
  const human = String(item.status_reason || '').trim()
  return raw === human ? '' : raw
}

export function rowClass(item: Account): string {
  const category = statusCategory(item)
  if (category === 'disabled') return 'bg-muted/50'
  if (category === 'abnormal') return 'bg-rose-500/5'
  if (category === 'limited') return 'bg-amber-500/5'
  if (!item.access_token && !item.cookie) return 'bg-muted/30'
  return ''
}

export function quotaLabel(key: QuotaKey): string {
  if (key === 'fast') return '文本 fast'
  if (key === 'thinking') return '文本 thinking'
  if (key === 'pro') return '文本 pro'
  if (key === 'image') return '图片'
  if (key === 'music') return '音乐'
  return '视频'
}

export function quotaLineText(line: QuotaLine): string {
  if (line.limit < 0 || !line.limited) return `${line.used}/∞`
  return `${line.used}/${line.limit}`
}

function isLowRemaining(line: QuotaLine): boolean {
  if (line.limit < 0 || !line.limited || line.remaining <= 0) return false
  const threshold = Math.max(1, Math.min(3, Math.floor(line.limit * 0.1)))
  return line.remaining <= threshold
}

export function quotaLineClass(item: Account, key: QuotaKey, line?: QuotaLine): string {
  const target = line || quotaLine(item, key)
  const state = getStateByQuotaKey(getGroupStates(item), key)

  if (state?.level === 'error') return PILL_TONE_CLASS.danger
  if (state?.level === 'warn' && !(target.limited && target.remaining <= 0)) return PILL_TONE_CLASS.warning
  if (target.limit < 0 || !target.limited) return PILL_TONE_CLASS.neutral
  if (target.remaining <= 0) return PILL_TONE_CLASS.danger
  if (isLowRemaining(target)) return PILL_TONE_CLASS.warning
  return PILL_TONE_CLASS.success
}

export function quotaSummaryClass(item: Account): string {
  if (item.lane_backoff_summary?.active) return PILL_TONE_CLASS.warning

  const proEnabled = Array.isArray(item.lanes) && item.lanes.includes('pro')
  if (!proEnabled) return PILL_TONE_CLASS.neutral

  const proState = getStateByQuotaKey(getGroupStates(item), 'pro')
  const proLine = quotaLine(item, 'pro')

  if (proState?.status === 'cooldown') return PILL_TONE_CLASS.warning
  if (proLine.limited && proLine.remaining <= 0) return PILL_TONE_CLASS.danger
  if (proLine.limit < 0 || !proLine.limited) return PILL_TONE_CLASS.neutral
  if (isLowRemaining(proLine)) return PILL_TONE_CLASS.warning
  return PILL_TONE_CLASS.success
}

export function quotaSummaryText(): string {
  return '图片额度'
}

export function laneEnabled(lanes: AccountLane[], lane: AccountLane): boolean {
  return lanes.includes(lane)
}

function laneCount(lanes: AccountLane[]): number {
  return laneOrder.filter((lane) => lanes.includes(lane)).length
}

export function laneSummaryClass(lanes: AccountLane[]): string {
  const enabledCount = laneCount(lanes)
  if (enabledCount === laneOrder.length) return PILL_TONE_CLASS.success
  if (enabledCount === 0) return PILL_TONE_CLASS.neutral
  return PILL_TONE_CLASS.warning
}

export function laneSummaryText(lanes: AccountLane[]): string {
  return `${laneCount(lanes)}/${laneOrder.length}`
}

export function laneLineClass(lane: AccountLane, lanes: AccountLane[]): string {
  if (!laneEnabled(lanes, lane)) return 'text-muted-foreground'
  if (lane === 'fast') return 'bg-emerald-500/10 text-emerald-700'
  if (lane === 'thinking') return 'bg-cyan-500/10 text-cyan-700'
  return 'bg-blue-500/10 text-blue-700'
}

export function accountPrimaryText(item: Account): string {
  return cleanString(item.email) || cleanString(item.user_id) || cleanString(item.name) || item.id
}

export function accountSecondaryText(item: Account): string {
  const userId = cleanString(item.user_id)
  const email = cleanString(item.email)
  if (email && userId) return userId
  return item.id
}

export function accountSourceText(item: Account): string {
  const type = cleanString(item.type) || 'free'
  const sourceType = cleanString(item.source_type) || 'web'
  return `${type} / ${sourceType}`
}

export function accountProxyText(item: Account): string {
  return proxyReferenceLabel(item.proxy)
}

export function accountTokenPreview(item: Account): string {
  const masked = cleanString(item.cookie)
  if (masked) return masked
  const token = cleanString(item.access_token)
  if (!token) return '缺失'
  if (token.length <= 12) return '********'
  return `${token.slice(0, 6)}...${token.slice(-4)}`
}

export function accountQuotaText(item: Account): string {
  if (item.image_quota_unknown) return '未知'
  return `${Math.max(0, Number(item.quota || 0))}`
}

export function accountCreatedText(item: Account): string {
  return formatAccountDate(item.created_at)
}

export function accountRestoreText(item: Account): string {
  return formatAccountDate(item.restore_at)
}

export function checkoutChannelText(value: unknown): string {
  return cleanString(value) ? 'UPI' : ''
}

export function checkoutFinalKindText(value: unknown): string {
  const kind = cleanString(value).toLowerCase()
  const labels: Record<string, string> = {
    upi_instructions: 'Stripe UPI 指引',
    stripe_upi_instructions: 'Stripe UPI 指引',
    stripe_upi: 'Stripe UPI 指引',
  }
  return labels[kind] || cleanString(value)
}

function checkoutPaymentMethodText(value: unknown): string {
  return cleanString(value) ? 'UPI' : ''
}

function checkoutAmountText(item: Account): string {
  const minor = item.checkout_amount_minor
  if (minor === undefined || minor === null || !Number.isFinite(Number(minor))) return ''
  const currency = cleanString(item.checkout_currency).toUpperCase()
  if (!currency) return `${Math.max(0, Math.trunc(Number(minor)))}`
  const fractionDigits = 2
  const amount = Math.max(0, Math.trunc(Number(minor))) / (10 ** fractionDigits)
  try {
    return new Intl.NumberFormat('zh-CN', {
      style: 'currency',
      currency,
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
    }).format(amount)
  } catch {
    return `${currency} ${amount.toFixed(fractionDigits)}`
  }
}

/**
 * The legacy `checkout_url` was an OpenAI bootstrap URL.  Only use it as a
 * final-link alias after the backend explicitly supplies a final-link kind.
 */
export function checkoutFinalLinkUrl(item: Account): string {
  const finalUrl = cleanString(item.checkout_final_url)
  if (finalUrl) return finalUrl
  return cleanString(item.checkout_final_kind) ? cleanString(item.checkout_url) : ''
}

export function checkoutFinalLinkStatusLabel(item: Account): string {
  if (checkoutFinalLinkUrl(item)) {
    const kind = checkoutFinalKindText(item.checkout_final_kind)
    return kind ? `已就绪 · ${kind}` : '已就绪'
  }
  const status = cleanString(item.checkout_link_status).toLowerCase()
  if (status === 'pending' || status === 'processing') return '提取中'
  if (status === 'failed') return '提取失败'
  return '未提取'
}

export function checkoutFinalLinkToneClass(item: Account): string {
  if (checkoutFinalLinkUrl(item)) return PILL_TONE_CLASS.success
  const status = cleanString(item.checkout_link_status).toLowerCase()
  if (status === 'failed') return PILL_TONE_CLASS.danger
  if (status === 'pending' || status === 'processing') return PILL_TONE_CLASS.warning
  return PILL_TONE_CLASS.neutral
}

/** A compact, list-friendly checkout state. Detailed protocol diagnostics stay in registration logs. */
export function checkoutExtractionStatusLabel(item: Account): string {
  if (checkoutFinalLinkUrl(item)) return '是'
  const status = cleanString(item.checkout_link_status).toLowerCase()
  if (status === 'pending' || status === 'processing') return '提链中'
  return '否'
}

export function checkoutExtractionToneClass(item: Account): string {
  return checkoutFinalLinkToneClass(item)
}

export function checkoutFinalLinkDetail(item: Account): string {
  const details = [
    `状态：${checkoutFinalLinkStatusLabel(item)}`,
  ]
  const channel = checkoutChannelText(item.checkout_channel)
  const country = cleanString(item.checkout_country).toUpperCase()
  const currency = cleanString(item.checkout_currency).toUpperCase()
  const paymentMethod = checkoutPaymentMethodText(item.checkout_payment_method)
  const amount = checkoutAmountText(item)
  if (channel) details.push(`渠道：${channel}`)
  if (country || currency) details.push(`地区：${[country, currency].filter(Boolean).join(' / ')}`)
  if (paymentMethod) details.push(`支付方式：${paymentMethod}`)
  if (item.checkout_is_free_trial) details.push('权益：免费试用')
  else if (amount) details.push(`金额：${amount}`)
  const qrExpiresAt = item.checkout_qr_expires_at_unix || item.checkout_qr_expires_at
  if (qrExpiresAt) {
    const numeric = Number(qrExpiresAt)
    const value = Number.isFinite(numeric) && numeric > 0
      ? formatAccountDate(numeric > 1e12 ? numeric / 1000 : numeric)
      : cleanString(qrExpiresAt)
    if (value) details.push(`二维码到期：${value}`)
  }
  if (!checkoutFinalLinkUrl(item) && cleanString(item.checkout_last_error)) {
    details.push(`错误：${cleanString(item.checkout_last_error)}`)
  }
  return details.join('\n')
}

export function accountStatusDetailText(
  item: Account,
  groupLabel: (groupId: string | undefined) => string,
  proxyText: (account: Account) => string = accountProxyText,
): string {
  return [
    statusReason(item),
    `账号组：${groupLabel(item.group_id)}`,
    `代理：${proxyText(item)}`,
  ].filter(Boolean).join('\n')
}

export function accountDetailItems(item: Account) {
  const survival = item.survival_status
    ? `${item.survival_status}${item.survival_observed_seconds ? ` · ${formatDuration(item.survival_observed_seconds)}` : ''}`
    : '-'
  return [
    { label: '创建时间', value: accountCreatedText(item) },
    { label: '恢复时间', value: accountRestoreText(item) },
    { label: '图片额度', value: accountQuotaText(item) },
    { label: '成功 / 失败', value: `${item.success_count || 0} / ${item.failure_count || 0}` },
    { label: '存活确认', value: survival },
  ]
}

export function accountGroupNameMap(groups: readonly AccountGroup[]): Map<string, string> {
  return new Map(groups.map((group) => [group.id, group.name || group.id]))
}

export function accountGroupLabel(groupId: string | undefined, groupNames: ReadonlyMap<string, string>): string {
  const id = cleanString(groupId)
  if (!id) return '未分组'
  return groupNames.get(id) || id
}

export function accountGroupProxyLabel(group: AccountGroup, proxyGroups: readonly ProxyGroup[]): string {
  const legacyProxyGroupId = cleanString(group.proxy_group_id)
  const proxyReference = parseProxyReference(group.proxy || (legacyProxyGroupId ? `group:${legacyProxyGroupId}` : ''))
  const proxyGroup = proxyReference.mode === 'group'
    ? proxyGroups.find((item) => item.id === proxyReference.value)
    : null
  if (proxyReference.mode === 'global') return '使用默认出口'
  if (proxyReference.mode === 'direct') return '强制直连'
  if (proxyReference.mode === 'group') return `代理组：${proxyGroup?.name || proxyReference.value || '-'}`
  if (proxyReference.mode === 'profile') return `历史代理：${proxyReference.value || '-'}`
  return `自定义代理：${proxyReference.value || '-'}`
}

export function buildAccountGroupRows(
  groups: readonly AccountGroup[],
  proxyGroups: readonly ProxyGroup[],
): AccountGroupRow[] {
  return groups.map((group) => ({
    ...group,
    raw: group,
    name: group.name || group.id,
    account_count: Number(group.account_count || 0),
    proxy_label: accountGroupProxyLabel(group, proxyGroups),
  }))
}

export function buildAccountProgressMetricItems(
  metricLabel: string,
  metricValue: string | number,
  statusTextValue: string,
): AccountProgressMetricItem[] {
  return [
    {
      key: 'metric',
      label: metricLabel,
      value: metricValue,
    },
    {
      key: 'status',
      label: '状态',
      value: statusTextValue,
    },
  ]
}
