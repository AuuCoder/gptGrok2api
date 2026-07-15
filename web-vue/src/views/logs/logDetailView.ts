import type { SystemLogRow } from '@/api/logs'
import {
  isSystemLogFailed as isFailed,
  isSystemLogLimited as isLimited,
  isSystemLogSuccess as isSuccess,
} from '@/api/logs'

export type DetailField = {
  label: string
  value: string
  copyable?: boolean
  wide?: boolean
}

export type DetailTone = 'success' | 'danger' | 'warning' | 'info' | 'muted'
export type DetailTimelineCategory = 'entry' | 'prepare' | 'network' | 'upstream' | 'resolve' | 'download' | 'retry' | 'response'

type DetailTimelineStepConfig = {
  key: string
  label: string
  group: string
  hint?: string
}

export type DetailTimelineStep = DetailTimelineStepConfig & {
  valueMs: number
  value: string
  tone: DetailTone
  category: DetailTimelineCategory
  statusLabel: string
  barStyle: Record<string, string>
  time: string
  note: string
}

export type DetailTimelineSegment = {
  key: string
  label: string
  valueMs: number
  value: string
  percent: string
  tone: DetailTone
  category: DetailTimelineCategory
  compact: boolean
  barStyle: Record<string, string>
  title: string
}

export type DetailTimelineLegendItem = {
  key: DetailTimelineCategory | DetailTone
  label: string
  category: DetailTimelineCategory | 'state'
  tone: DetailTone
}

export type DetailTimelineGroup = {
  name: string
  steps: DetailTimelineStep[]
}

const detailTimelineSteps: DetailTimelineStepConfig[] = [
  { key: 'handler_queue_ms', label: '等待入口', group: '入口与账号', hint: 'run_in_threadpool' },
  { key: 'stream_first_queue_ms', label: '读取首包', group: '入口与账号', hint: '首个响应事件' },
  { key: 'account_wait_ms', label: '等待账号', group: '入口与账号', hint: '账号池筛选' },
  { key: 'egress_wait_ms', label: '等待出口', group: '入口与账号', hint: '代理出口准备' },
  { key: 'egress_acquire_ms', label: '出口租约', group: '入口与账号', hint: '代理节点并发' },
  { key: 'upload_ms', label: '上传输入图', group: '上游准备', hint: '参考图上传' },
  { key: 'bootstrap_ms', label: '预热页面', group: '上游准备', hint: '上游会话页面' },
  { key: 'requirements_ms', label: '获取请求令牌', group: '上游准备', hint: 'requirements / token' },
  { key: 'prepare_conversation_ms', label: '准备会话', group: '上游准备', hint: '图片会话上下文' },
  { key: 'generation_start_ms', label: '启动生成', group: '上游准备', hint: '提交上游请求' },
  { key: 'http_dns_ms', label: 'HTTP DNS', group: 'HTTP 连接', hint: '域名解析' },
  { key: 'http_tcp_ms', label: 'HTTP TCP', group: 'HTTP 连接', hint: '代理 / TCP 建连' },
  { key: 'http_tls_ms', label: 'HTTP TLS', group: 'HTTP 连接', hint: 'TLS 握手' },
  { key: 'http_wait_ms', label: 'HTTP 等待', group: 'HTTP 连接', hint: '请求发出到首包' },
  { key: 'http_ttfb_ms', label: 'HTTP 首包', group: 'HTTP 连接', hint: '请求开始到首包' },
  { key: 'sse_first_event_ms', label: 'SSE 首事件', group: '生成与结果', hint: '首个 data 事件' },
  { key: 'sse_max_gap_ms', label: 'SSE 最大空窗', group: '生成与结果', hint: '两次事件最大间隔' },
  { key: 'sse_last_gap_ms', label: 'SSE 收尾空窗', group: '生成与结果', hint: '最后事件到关闭' },
  { key: 'conversation_stream_ms', label: '上游生成', group: '生成与结果', hint: '供应商会话流' },
  { key: 'stream_error_ms', label: '上游断流', group: '生成与结果', hint: 'HTTP2 / SSE' },
  { key: 'resolve_ms', label: '解析/轮询', group: '生成与结果', hint: 'conversation / file' },
  { key: 'download_ms', label: '下载图片', group: '生成与结果', hint: '图片文件下载' },
  { key: 'retry_wait_ms', label: '重试等待', group: '生成与结果', hint: '轮询 / 退避' },
  { key: 'response_ms', label: '响应整理', group: '生成与结果', hint: 'Codex 响应' },
  { key: 'stream_ms', label: '单图内部', group: '生成与结果', hint: '单图链路' },
  { key: 'total_ms', label: '单图总耗时', group: '生成与结果', hint: '完整链路' },
]

const detailTimelineGroupOrder = ['入口与账号', '上游准备', 'HTTP 连接', '生成与结果']
const detailTimelineAggregateKeys = new Set([
  'http_dns_ms',
  'http_tcp_ms',
  'http_tls_ms',
  'http_wait_ms',
  'http_ttfb_ms',
  'sse_first_event_ms',
  'sse_max_gap_ms',
  'sse_last_gap_ms',
  'stream_ms',
  'total_ms',
])
const defaultTimelineWarningThresholdMs = 60_000
const timelineWarningThresholdMs: Record<string, number> = {
  handler_queue_ms: 1_000,
  stream_first_queue_ms: 1_000,
  account_wait_ms: 10_000,
  egress_wait_ms: 10_000,
  egress_acquire_ms: 10_000,
  upload_ms: 60_000,
  bootstrap_ms: 60_000,
  requirements_ms: 60_000,
  prepare_conversation_ms: 60_000,
  generation_start_ms: 60_000,
  http_dns_ms: 1_000,
  http_tcp_ms: 3_000,
  http_tls_ms: 5_000,
  http_wait_ms: 30_000,
  http_ttfb_ms: 30_000,
  sse_first_event_ms: 30_000,
  sse_max_gap_ms: 60_000,
  sse_last_gap_ms: 30_000,
  download_ms: 60_000,
  retry_wait_ms: 60_000,
  response_ms: 30_000,
}

const detailTimelineCategoryLabels: Record<DetailTimelineCategory, string> = {
  entry: '入口与账号',
  prepare: '上游准备',
  network: 'HTTP 连接',
  upstream: '上游生成',
  resolve: '解析/轮询',
  download: '图片下载',
  retry: '重试等待',
  response: '响应整理',
}
const detailTimelineCategoryOrder: DetailTimelineCategory[] = ['entry', 'prepare', 'network', 'upstream', 'resolve', 'download', 'retry', 'response']

function cleanString(value: unknown): string {
  return String(value || '').trim()
}

export function detailRecord(item: SystemLogRow): Record<string, any> {
  const detail = item.raw.detail
  return detail && typeof detail === 'object' ? detail : {}
}

function monitorRecord(item: SystemLogRow): Record<string, any> {
  const monitor = detailRecord(item).monitor
  return monitor && typeof monitor === 'object' ? monitor : {}
}

function rawDetailValue(item: SystemLogRow, key: string): string {
  return formatInlineValue(detailRecord(item)[key])
}

function rawMonitorValue(item: SystemLogRow, key: string): string {
  return formatInlineValue(monitorRecord(item)[key])
}

function isErrorStatusCode(value: string): boolean {
  const statusCode = Number(cleanString(value))
  return Number.isFinite(statusCode) && statusCode >= 400
}

function diagnosticStageValue(item: SystemLogRow): string {
  const stage = item.stage || rawMonitorValue(item, 'stage_label') || rawMonitorValue(item, 'stage')
  const normalized = cleanString(stage).toLowerCase()
  if (isSuccess(item) && ['success', 'completed', 'complete', 'done', '完成'].includes(normalized)) return ''
  return stage
}

function hasDiagnosticSignal(item: SystemLogRow): boolean {
  return Boolean(
    isFailed(item)
      || isLimited(item)
      || isErrorStatusCode(item.statusCode)
      || item.errorCode
      || item.error
      || item.reason
      || item.upstreamErrorType
      || item.upstreamRequestId
      || item.blocked === '是'
      || item.toolInvoked === '是'
      || diagnosticStageValue(item),
  )
}

export function formatInlineValue(value: unknown): string {
  if (value === undefined || value === null || value === '') return ''
  if (Array.isArray(value)) return value.map(formatInlineValue).filter(Boolean).join(' · ')
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>).filter(([, item]) => item !== undefined && item !== null && item !== '')
    if (!entries.length) return ''
    const primitive = entries.every(([, item]) => !item || ['string', 'number', 'boolean'].includes(typeof item))
    if (primitive && entries.length <= 8) {
      return entries.map(([key, item]) => `${key}: ${formatInlineValue(item)}`).join(' · ')
    }
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return String(value)
    }
  }
  return String(value).trim()
}

export function formatTimelineMs(value: unknown): string {
  const ms = Number(value || 0)
  if (!Number.isFinite(ms) || ms <= 0) return '-'
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)}m`
  if (ms >= 10_000) return `${(ms / 1000).toFixed(1)}s`
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`
  return `${Math.round(ms)}ms`
}

function metricFromRecord(record: unknown, key: string): number {
  if (!record || typeof record !== 'object') return 0
  const raw = (record as Record<string, unknown>)[key]
  const parsed = Number(raw || 0)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0
}

export function metricValueFromLog(item: SystemLogRow, key: string): number {
  const detail = detailRecord(item)
  const monitor = monitorRecord(item)
  const values = [
    metricFromRecord(detail.perf, key),
    metricFromRecord(detail.metrics, key),
    metricFromRecord(monitor.metrics, key),
  ]
  const images = monitor.images
  if (images && typeof images === 'object') {
    Object.values(images as Record<string, any>).forEach((image) => {
      if (image && typeof image === 'object') values.push(metricFromRecord(image.metrics, key))
    })
  }
  return Math.max(...values, 0)
}

function timelineStepCategory(key: string, group: string): DetailTimelineCategory {
  if (group === '入口与账号') return 'entry'
  if (group === '上游准备') return 'prepare'
  if (group === 'HTTP 连接') return 'network'
  if (key === 'sse_first_event_ms' || key === 'sse_max_gap_ms' || key === 'sse_last_gap_ms') return 'upstream'
  if (key === 'conversation_stream_ms') return 'upstream'
  if (key === 'resolve_ms') return 'resolve'
  if (key === 'download_ms') return 'download'
  if (key === 'retry_wait_ms') return 'retry'
  return 'response'
}

function timelineStepTone(key: string, valueMs: number): DetailTone {
  if (key === 'stream_error_ms') return 'danger'
  const threshold = timelineWarningThresholdMs[key] ?? defaultTimelineWarningThresholdMs
  if (valueMs >= threshold) return 'warning'
  return 'info'
}

function timelineStatusLabel(tone: DetailTone): string {
  if (tone === 'danger') return '异常'
  if (tone === 'warning') return '慢'
  if (tone === 'success') return '完成'
  if (tone === 'info') return '记录'
  return '记录'
}

function eventTimeForMetric(item: SystemLogRow, metricKey: string): string {
  const events = monitorRecord(item).events
  if (!Array.isArray(events)) return ''
  const matched = events.find((event) => event && typeof event === 'object' && Number((event as Record<string, unknown>)[metricKey] || 0) > 0)
  return formatInlineValue((matched as Record<string, unknown> | undefined)?.time)
}

function requestShapeImageSummary(item: SystemLogRow): string {
  const shape = detailRecord(item).request_shape
  if (!shape || typeof shape !== 'object') return ''
  const record = shape as Record<string, unknown>
  const pairs: Array<[string, string]> = [
    ['input_image_parts', '输入图'],
    ['image_url_parts', '图链'],
    ['image_parts', '图片块'],
    ['data_url_images', 'base64'],
    ['remote_image_urls', '远程图'],
    ['literal_image_placeholders', '占位图'],
  ]
  const parts = pairs
    .map(([key, label]) => [label, Number(record[key] || 0)] as const)
    .filter(([, count]) => Number.isFinite(count) && count > 0)
    .map(([label, count]) => `${label} ${count}`)
  return parts.join(' · ')
}

function timelineStepNote(item: SystemLogRow, step: DetailTimelineStepConfig): string {
  const parts = [step.hint || '']
  if (step.key === 'upload_ms') parts.push(requestShapeImageSummary(item))
  if (step.key === 'resolve_ms' && item.imageUrls.length) parts.push(`结果图 ${item.imageUrls.length}`)
  if (step.key === 'download_ms' && item.imageUrls.length) parts.push(`下载 ${item.imageUrls.length} 张`)
  return parts.filter(Boolean).join(' · ')
}

function maskKeyLabel(value: string): string {
  return cleanString(value).replace(/sk-[A-Za-z0-9_-]{6,}/g, (token) => `${token.slice(0, 5)}***${token.slice(-4)}`)
}

function proxySourceLabel(value: unknown): string {
  const source = cleanString(value)
  if (!source) return ''
  if (source.includes('account_group')) return '账号组'
  if (source.includes('account')) return '账号'
  if (source.includes('default')) return '默认'
  if (source.includes('global')) return '默认'
  if (source.includes('runtime_resource')) return '资源代理'
  if (source.includes('runtime')) return 'Runtime'
  if (source.includes('explicit')) return '指定'
  if (source.includes('direct')) return '直连'
  return source
}

function egressDetailValue(item: SystemLogRow): string {
  const source = item.proxySource || rawMonitorValue(item, 'proxy_source')
  const hash = item.proxyHash || rawMonitorValue(item, 'proxy_hash')
  const groupId = item.proxyGroupId || rawMonitorValue(item, 'proxy_group_id')
  const nodeName = item.proxyNodeName || rawMonitorValue(item, 'proxy_node_name')
  const nodeId = item.proxyNodeId || rawMonitorValue(item, 'proxy_node_id')
  const egressLabel = item.egressLabel || rawMonitorValue(item, 'egress_label')
  const label = proxySourceLabel(source)
  if (!label) return ''
  const nodeLabel = [groupId, nodeName || nodeId].filter(Boolean).join('/')
  if (nodeLabel) return `${label} ${nodeLabel}`
  if (egressLabel && egressLabel !== 'direct' && !egressLabel.startsWith('proxy:')) return `${label} ${egressLabel}`
  if (hash && hash !== 'direct') return `${label} ${hash}`
  return label
}

function hasDetailValue(value: string): boolean {
  const clean = cleanString(value)
  return Boolean(clean && clean !== '-' && clean.toLowerCase() !== 'null' && clean.toLowerCase() !== 'undefined')
}

function compactDetailFields(fields: Array<DetailField | null>, options: { keepStatus?: boolean } = {}): DetailField[] {
  return fields.filter((field): field is DetailField => {
    if (!field) return false
    if (options.keepStatus && field.label === '状态') return true
    return hasDetailValue(field.value)
  })
}

function timeRangeDetailValue(item: SystemLogRow): string {
  const start = cleanString(item.startedAt || item.time)
  const end = cleanString(item.endedAt)
  if (!start) return end
  if (!end || end === start) return start
  return `${start} → ${end}`
}

export function buildTimelineSegments(item: SystemLogRow | null): DetailTimelineSegment[] {
  if (!item) return []
  const rawSegments = detailTimelineSteps
    .filter((step) => !detailTimelineAggregateKeys.has(step.key))
    .map((step) => ({
      ...step,
      valueMs: metricValueFromLog(item, step.key),
    }))
    .filter((step) => step.valueMs > 0)
  const totalMs = rawSegments.reduce((total, step) => total + step.valueMs, 0)
  if (totalMs <= 0) return []
  return rawSegments.map((step) => {
    const percent = (step.valueMs / totalMs) * 100
    const tone = timelineStepTone(step.key, step.valueMs)
    const category = timelineStepCategory(step.key, step.group)
    const value = formatTimelineMs(step.valueMs)
    return {
      key: step.key,
      label: step.label,
      valueMs: step.valueMs,
      value,
      percent: `${percent.toFixed(percent >= 10 ? 0 : 1)}%`,
      tone,
      category,
      compact: percent < 12,
      barStyle: { flexGrow: String(Math.max(step.valueMs, 1)) },
      title: `${step.label} ${value} · ${percent.toFixed(1)}%`,
    }
  })
}

export function buildTimelineLegendItems(segments: DetailTimelineSegment[]): DetailTimelineLegendItem[] {
  if (!segments.length) return []
  const categories = new Set(segments.map((segment) => segment.category))
  const items: DetailTimelineLegendItem[] = detailTimelineCategoryOrder
    .filter((category) => categories.has(category))
    .map((category) => ({
      key: category,
      label: detailTimelineCategoryLabels[category],
      category,
      tone: 'info',
    }))
  if (segments.some((segment) => segment.tone === 'warning')) {
    items.push({ key: 'warning', label: '超过阈值', category: 'state', tone: 'warning' })
  }
  if (segments.some((segment) => segment.tone === 'danger')) {
    items.push({ key: 'danger', label: '异常中断', category: 'state', tone: 'danger' })
  }
  return items
}

export function buildTimelineGroups(item: SystemLogRow | null): DetailTimelineGroup[] {
  if (!item) return []
  const maxMs = Math.max(...detailTimelineSteps.map((step) => metricValueFromLog(item, step.key)), 0)
  if (maxMs <= 0) return []
  const groups = new Map<string, DetailTimelineStep[]>()
  detailTimelineSteps.forEach((step) => {
    const valueMs = metricValueFromLog(item, step.key)
    if (valueMs <= 0) return
    const width = Math.max(3, Math.round((valueMs / maxMs) * 100))
    const tone = timelineStepTone(step.key, valueMs)
    const category = timelineStepCategory(step.key, step.group)
    const groupSteps = groups.get(step.group) || []
    groupSteps.push({
      ...step,
      valueMs,
      value: formatTimelineMs(valueMs),
      tone,
      category,
      statusLabel: timelineStatusLabel(tone),
      barStyle: { width: `${width}%` },
      time: eventTimeForMetric(item, step.key),
      note: timelineStepNote(item, step),
    })
    groups.set(step.group, groupSteps)
  })
  return detailTimelineGroupOrder
    .map((name) => ({ name, steps: groups.get(name) || [] }))
    .filter((group) => group.steps.length > 0)
}

export function shouldAutoExpandTimeline(item: SystemLogRow | null, bottleneckStep: DetailTimelineStep | null): boolean {
  if (!item) return false
  if (isFailed(item)) return true
  if (Number(item.durationMs || 0) >= 180_000) return true
  if (metricValueFromLog(item, 'stream_error_ms') > 0) return true
  return bottleneckStep?.tone === 'danger'
}

export function buildPrimaryDetailFields(item: SystemLogRow | null): DetailField[] {
  if (!item) return []
  return compactDetailFields([
    { label: '请求 ID', value: rawDetailValue(item, 'call_id') || item.id, copyable: true },
    { label: '接口', value: item.endpoint, copyable: true },
    { label: '模型', value: item.model, copyable: true },
    { label: '账号', value: item.accountEmail, copyable: true },
    { label: 'OAuth 账号 ID', value: item.accountId, copyable: true },
    { label: '密钥', value: maskKeyLabel([item.keyName, item.keyId].filter(Boolean).join(' / ')) },
    { label: '出口', value: egressDetailValue(item) },
    { label: '会话 ID', value: item.conversationId, copyable: true },
    { label: '时间', value: timeRangeDetailValue(item), wide: true },
  ])
}

export function buildDiagnosticDetailFields(item: SystemLogRow | null): DetailField[] {
  if (!item) return []
  if (!hasDiagnosticSignal(item)) return []
  const shouldShowBooleans = isFailed(item) || isLimited(item) || Boolean(item.errorCode || item.error || item.reason)
  return compactDetailFields([
    { label: '状态码', value: item.statusCode },
    { label: '错误码', value: item.errorCode, copyable: true },
    { label: '阶段', value: diagnosticStageValue(item), copyable: true },
    { label: '原因', value: item.reason, copyable: true },
    { label: '上游错误', value: item.upstreamErrorType, copyable: true },
    { label: '上游请求 ID', value: item.upstreamRequestId, copyable: true },
    { label: '请求形状', value: item.requestShape, copyable: true },
    shouldShowBooleans ? { label: '工具调用', value: item.toolInvoked } : null,
    shouldShowBooleans ? { label: '阻断', value: item.blocked } : null,
    { label: '上游文本长度', value: item.upstreamMessageLen },
  ])
}
