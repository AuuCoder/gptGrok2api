import type { GrokAccount } from '@/api/grokAccounts'
import { PILL_TONE_CLASS } from '@/lib/pillTones'

function cleanString(value: unknown): string {
  return String(value || '').trim()
}

export function grokRefreshFailed(item: GrokAccount): boolean {
  return cleanString(item.refresh_status).toLowerCase() === 'failed'
}

export function grokRefreshStatusTitle(item: GrokAccount): string {
  if (!grokRefreshFailed(item)) return ''
  const error = cleanString(item.refresh_error) || '上游未返回真实额度数据'
  const refreshedAt = formatGrokAccountDate(item.refresh_at)
  return refreshedAt === '-' ? error : `${error}（${refreshedAt}）`
}

export function grokAccountStatusText(item: GrokAccount): string {
  const status = cleanString(item.status).toLowerCase()
  if (status === 'active') return '可用'
  if (status === 'pending_sso') return '待登录态'
  if (status === 'submission_failed') return '提交失败'
  if (status === 'submission_unknown') return '提交结果未知'
  if (status === 'submission_unconfirmed') return '提交待确认'
  if (status === 'submitting') return '提交中'
  if (status === 'pending_submit') return '待提交'
  return cleanString(item.status) || '未知'
}

export function grokAccountStatusClass(item: GrokAccount): string {
  const status = cleanString(item.status).toLowerCase()
  if (status === 'active') return PILL_TONE_CLASS.success
  if (status === 'submission_failed') return PILL_TONE_CLASS.danger
  if (
    status === 'pending_sso'
    || status === 'submission_unknown'
    || status === 'submission_unconfirmed'
    || status === 'submitting'
    || status === 'pending_submit'
  ) return PILL_TONE_CLASS.warning
  return PILL_TONE_CLASS.neutral
}

export function grokRuntimeStatusText(item: GrokAccount): string {
  const status = cleanString(item.runtime_status).toLowerCase()
  if (!status) return item.sync_state === 'synced' ? '待刷新' : '未加入'
  if (status === 'active') return '正常'
  if (status === 'cooling' || status === 'rate_limited') return '限流'
  if (status === 'invalid' || status === 'expired') return '异常'
  if (status === 'disabled') return '禁用'
  if (status === 'active' && grokRefreshFailed(item)) return '刷新失败'
  return cleanString(item.runtime_status)
}

export function grokRuntimeStatusClass(item: GrokAccount): string {
  const status = cleanString(item.runtime_status).toLowerCase()
  if (status === 'active') return PILL_TONE_CLASS.success
  if (status === 'cooling' || status === 'rate_limited') return PILL_TONE_CLASS.warning
  if (status === 'invalid' || status === 'expired') return PILL_TONE_CLASS.danger
  if (status === 'active' && grokRefreshFailed(item)) return PILL_TONE_CLASS.warning
  return PILL_TONE_CLASS.neutral
}

export function grokSyncStateText(item: GrokAccount): string {
  if (item.sync_state === 'synced') return '已加入'
  if (item.sync_state === 'not_ready') return '登录态未就绪'
  if (item.sync_state === 'not_synced') return '待加入'
  if (item.sync_state === 'runtime_unavailable') return '运行时不可用'
  if (item.sync_state === 'sync_failed' || item.sync_state === 'failed') return '加入失败'
  return '状态未知'
}

export function grokOAuthStatusText(item: GrokAccount): string {
  const status = cleanString(item.oauth?.status).toLowerCase()
  if (!status) return ''
  if (status === 'active') return 'OAuth'
  if (status === 'disabled') return 'OAuth 已禁用'
  if (status === 'expired') return 'OAuth 已过期'
  if (status === 'invalid') return 'OAuth 异常'
  return `OAuth ${status}`
}

export function grokOAuthStatusClass(item: GrokAccount): string {
  const status = cleanString(item.oauth?.status).toLowerCase()
  if (status === 'active') return PILL_TONE_CLASS.success
  if (status === 'expired' || status === 'invalid') return PILL_TONE_CLASS.danger
  return PILL_TONE_CLASS.neutral
}

export function grokAccountRowClass(item: GrokAccount): string {
  const status = cleanString(item.status).toLowerCase()
  const runtimeStatus = cleanString(item.runtime_status).toLowerCase()
  if (runtimeStatus === 'disabled') return 'bg-muted/50'
  if (runtimeStatus === 'invalid' || runtimeStatus === 'expired') return 'bg-rose-500/5'
  if (runtimeStatus === 'cooling' || runtimeStatus === 'rate_limited') return 'bg-amber-500/5'
  if (grokRefreshFailed(item)) return 'bg-amber-500/5'
  if (status === 'submission_failed') return 'bg-rose-500/5'
  if (status && status !== 'active') return 'bg-amber-500/5'
  return ''
}

export function grokCredentialText(present: boolean, kind: 'sso' | 'password'): string {
  if (kind === 'sso') return present ? 'SSO 已就绪' : 'SSO 缺失'
  return present ? '密码已保存' : '密码缺失'
}

export function grokCredentialClass(present: boolean): string {
  return present ? PILL_TONE_CLASS.success : PILL_TONE_CLASS.danger
}

export function grokAccountSourceText(item: GrokAccount): string {
  const source = cleanString(item.source_type)
  return source === 'protocol' ? '纯协议注册' : (source || '-')
}

export function grokAccountTokenPreview(item: GrokAccount): string {
  return cleanString(item.token_preview) || cleanString(item.id) || (item.has_sso ? 'SSO 已保存' : 'SSO 缺失')
}

export function grokAccountPoolText(item: GrokAccount): string {
  const pool = cleanString(item.pool).toLowerCase()
  if (pool === 'basic') return 'Basic'
  if (pool === 'super') return 'Super'
  if (pool === 'heavy') return 'Heavy'
  if (pool === 'auto') return 'Auto'
  return cleanString(item.pool) || '-'
}

export function formatGrokAccountDate(value: unknown): string {
  const raw = cleanString(value)
  if (!raw) return '-'
  const numeric = Number(raw)
  const date = Number.isFinite(numeric) && numeric > 0
    ? new Date(numeric > 10_000_000_000 ? numeric : numeric * 1000)
    : new Date(raw)
  if (Number.isNaN(date.getTime())) return raw
  const yyyy = date.getFullYear()
  const mm = String(date.getMonth() + 1).padStart(2, '0')
  const dd = String(date.getDate()).padStart(2, '0')
  const hh = String(date.getHours()).padStart(2, '0')
  const mi = String(date.getMinutes()).padStart(2, '0')
  return `${yyyy}-${mm}-${dd} ${hh}:${mi}`
}

type GrokQuotaMode = 'auto' | 'fast' | 'expert' | 'heavy' | 'console'

const GROK_QUOTA_MODE_LABELS: Record<GrokQuotaMode, string> = {
  auto: 'A',
  fast: 'F',
  expert: 'E',
  heavy: 'H',
  console: 'C',
}

function quotaRemaining(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.max(0, Math.trunc(value))
  if (!value || typeof value !== 'object') return null
  const source = value as Record<string, unknown>
  const remaining = Number(source.remaining)
  return Number.isFinite(remaining) ? Math.max(0, Math.trunc(remaining)) : null
}

export function grokQuotaEntries(item: GrokAccount): Array<{ key: GrokQuotaMode; label: string; remaining: number }> {
  if (!item.quota || typeof item.quota !== 'object') return []
  const quota = item.quota as Record<string, unknown>
  return (Object.keys(GROK_QUOTA_MODE_LABELS) as GrokQuotaMode[])
    .map((key) => ({ key, label: GROK_QUOTA_MODE_LABELS[key], remaining: quotaRemaining(quota[key]) }))
    .filter((entry): entry is { key: GrokQuotaMode; label: string; remaining: number } => entry.remaining !== null)
}

export function grokQuotaText(item: GrokAccount): string {
  const entries = grokQuotaEntries(item)
  if (!entries.length) return '-'
  return entries.map((entry) => `${entry.label} ${entry.remaining}`).join(' · ')
}

export function grokSuccessRate(item: GrokAccount): string {
  if (item.sync_state !== 'synced') return '-'
  const success = Math.max(0, Number(item.use_count || 0))
  const failure = Math.max(0, Number(item.fail_count || 0))
  const total = success + failure
  if (!total) return '-'
  return `${Math.round((success / total) * 100)}%`
}

export function grokUsageText(item: GrokAccount): string {
  if (item.sync_state !== 'synced') return '- / -'
  return `${Math.max(0, Number(item.use_count || 0))} / ${Math.max(0, Number(item.fail_count || 0))}`
}

export function grokAccountDetailItems(item: GrokAccount) {
  return [
    { label: 'Token', value: grokAccountTokenPreview(item) },
    { label: '类型', value: grokAccountPoolText(item) },
    { label: '注册状态', value: grokAccountStatusText(item) },
    { label: '运行状态', value: grokRuntimeStatusText(item) },
    { label: '额度', value: grokQuotaText(item) },
    { label: '成功 / 失败', value: grokUsageText(item) },
    { label: '成功率', value: grokSuccessRate(item) },
    { label: '最近使用', value: formatGrokAccountDate(item.last_used_at) },
    { label: '最近刷新', value: formatGrokAccountDate(item.refresh_at) },
    {
      label: '刷新结果',
      value: grokRefreshFailed(item)
        ? (cleanString(item.refresh_error) || '刷新失败')
        : cleanString(item.refresh_status) === 'success' ? '成功' : '-',
    },
  ]
}

export function grokAccountRowSignature(item: GrokAccount): string {
  return [
    item.id,
    item.email,
    item.status,
    item.source_type,
    item.token_preview,
    item.pool,
    item.runtime_status,
    JSON.stringify(item.quota || {}),
    item.use_count || 0,
    item.fail_count || 0,
    item.last_used_at,
    item.refresh_status,
    item.refresh_at,
    item.refresh_error,
    (item.tags || []).join(','),
    item.sync_state,
    item.oauth?.id,
    item.oauth?.status,
    (item.oauth?.models || []).join(','),
    item.oauth?.expires_at,
    item.has_sso ? 1 : 0,
    item.has_password ? 1 : 0,
    item.created_at,
    item.updated_at,
  ].map((value) => cleanString(value).replaceAll('|', '/')).join('|')
}
