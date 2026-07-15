import type {
  MonitorMetricMap,
  RealtimeMonitorEvent,
  RealtimeMonitorRecord,
  RealtimeMonitorResponse,
  RealtimeMonitorSummary,
} from '@/api/monitor'

export type BadgeTone = 'success' | 'danger' | 'warning' | 'info' | 'muted'

export type MonitorSlowMetricItem = {
  key: string
  label: string
  raw: number
  value: string
  important: boolean
}

export type MonitorDiagnosticItem = {
  key: string
  label: string
  value: string | number
  meta?: string
  valueClass: string
}

export type MonitorDiagnosticGroup = {
  key: string
  title: string
  meta: string
  items: MonitorDiagnosticItem[]
}

export type MonitorStageCountItem = {
  label: string
  count: number
}

const ENTRY_QUEUE_METRIC_KEYS = ['handler_queue_ms', 'stream_first_queue_ms'] as const
const ENTRY_ACCOUNT_METRIC_KEYS = ['handler_queue_ms', 'stream_first_queue_ms', 'account_wait_ms', 'egress_wait_ms'] as const

const DIGEST_METRIC_PAIRS = [
  ['等待入口', 'handler_queue_ms'],
  ['首包', 'stream_first_queue_ms'],
  ['等待账号', 'account_wait_ms'],
  ['等待出口', 'egress_wait_ms'],
  ['出口租约', 'egress_acquire_ms'],
  ['上传', 'upload_ms'],
  ['初始化', 'bootstrap_ms'],
  ['令牌', 'requirements_ms'],
  ['准备', 'prepare_conversation_ms'],
  ['启动', 'generation_start_ms'],
  ['HTTP首包', 'http_ttfb_ms'],
  ['HTTP等待', 'http_wait_ms'],
  ['SSE首事件', 'sse_first_event_ms'],
  ['SSE空窗', 'sse_max_gap_ms'],
  ['上游生成', 'conversation_stream_ms'],
  ['上游断流', 'stream_error_ms'],
  ['解析/轮询', 'resolve_ms'],
  ['下载', 'download_ms'],
  ['重试等待', 'retry_wait_ms'],
  ['单图链路', 'stream_ms'],
] as const

const SLOW_METRIC_PAIRS = [
  { key: 'handler_queue_ms', label: '等待入口' },
  { key: 'stream_first_queue_ms', label: '首包' },
  { key: 'account_wait_ms', label: '等待账号' },
  { key: 'egress_wait_ms', label: '等待出口' },
  { key: 'egress_acquire_ms', label: '出口租约' },
  { key: 'upload_ms', label: '上传' },
  { key: 'bootstrap_ms', label: '初始化' },
  { key: 'requirements_ms', label: '令牌' },
  { key: 'prepare_conversation_ms', label: '准备' },
  { key: 'generation_start_ms', label: '启动' },
  { key: 'http_dns_ms', label: 'HTTP DNS' },
  { key: 'http_tcp_ms', label: 'HTTP TCP' },
  { key: 'http_tls_ms', label: 'HTTP TLS' },
  { key: 'http_wait_ms', label: 'HTTP 等待' },
  { key: 'http_ttfb_ms', label: 'HTTP 首包' },
  { key: 'sse_first_event_ms', label: 'SSE 首事件' },
  { key: 'sse_max_gap_ms', label: 'SSE 最大空窗' },
  { key: 'sse_last_gap_ms', label: 'SSE 收尾空窗' },
  { key: 'conversation_stream_ms', label: '上游生成' },
  { key: 'stream_error_ms', label: '上游断流' },
  { key: 'resolve_ms', label: '解析/轮询' },
  { key: 'download_ms', label: '下载' },
  { key: 'retry_wait_ms', label: '重试等待' },
  { key: 'response_ms', label: '响应整理' },
  { key: 'stream_ms', label: '单图内部' },
  { key: 'total_ms', label: '单图总耗时' },
]

const EVENT_METRIC_PAIRS = [
  ['等待入口', 'handler_queue_ms'],
  ['首包', 'stream_first_queue_ms'],
  ['等待账号', 'account_wait_ms'],
  ['等待出口', 'egress_wait_ms'],
  ['上传', 'upload_ms'],
  ['初始化', 'bootstrap_ms'],
  ['令牌', 'requirements_ms'],
  ['准备', 'prepare_conversation_ms'],
  ['启动', 'generation_start_ms'],
  ['HTTP首包', 'http_ttfb_ms'],
  ['HTTP等待', 'http_wait_ms'],
  ['SSE首事件', 'sse_first_event_ms'],
  ['SSE空窗', 'sse_max_gap_ms'],
  ['上游生成', 'conversation_stream_ms'],
  ['上游断流', 'stream_error_ms'],
  ['解析/轮询', 'resolve_ms'],
  ['下载', 'download_ms'],
  ['重试等待', 'retry_wait_ms'],
  ['响应整理', 'response_ms'],
  ['单图内部', 'stream_ms'],
  ['单图总耗时', 'total_ms'],
] as const

const RECORD_SIGNATURE_METRIC_KEYS = [
  'handler_queue_ms',
  'stream_first_queue_ms',
  'account_wait_ms',
  'egress_wait_ms',
  'egress_acquire_ms',
  'upload_ms',
  'bootstrap_ms',
  'requirements_ms',
  'prepare_conversation_ms',
  'generation_start_ms',
  'http_dns_ms',
  'http_tcp_ms',
  'http_tls_ms',
  'http_wait_ms',
  'http_ttfb_ms',
  'sse_first_event_ms',
  'sse_max_gap_ms',
  'sse_last_gap_ms',
  'conversation_stream_ms',
  'stream_error_ms',
  'resolve_ms',
  'download_ms',
  'retry_wait_ms',
  'response_ms',
  'stream_ms',
  'total_ms',
] as const

export function formatMs(value: unknown) {
  const ms = Number(value || 0)
  if (!Number.isFinite(ms) || ms <= 0) return '-'
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)}m`
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.round(ms)}ms`
}

export function shortCallId(value: unknown) {
  const text = String(value || '')
  return text ? text.slice(0, 8) : '-'
}

export function maxMetricFromMap(map: MonitorMetricMap | undefined, keys: readonly string[]) {
  return keys.reduce((max, key) => Math.max(max, Number(map?.[key] || 0)), 0)
}

export function sumMetricFromMap(map: MonitorMetricMap | undefined, keys: readonly string[]) {
  return keys.reduce((sum, key) => sum + Math.max(0, Number(map?.[key] || 0)), 0)
}

export function metricValue(row: RealtimeMonitorRecord, key: string) {
  const perf = row.perf || {}
  const metrics = row.metrics || {}
  return Math.max(Number(perf[key] || 0), Number(metrics[key] || 0))
}

export function proxySourceLabel(value: unknown) {
  const source = String(value || 'direct')
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

export function egressLabelText(row: RealtimeMonitorRecord) {
  const groupId = String(row.proxy_group_id || '').trim()
  const nodeName = String(row.proxy_node_name || '').trim()
  const nodeId = String(row.proxy_node_id || '').trim()
  const nodeLabel = [groupId, nodeName || nodeId].filter(Boolean).join('/')
  if (nodeLabel) return nodeLabel
  const value = String(row.egress_label || '').trim()
  const source = String(row.proxy_source || '').trim()
  if (!value || value === 'direct') return ''
  if (value === source || value === `${source}_profile`) return ''
  if (value.startsWith('proxy:')) return ''
  return value
}

export function egressText(row: RealtimeMonitorRecord) {
  const label = proxySourceLabel(row.proxy_source)
  const egressLabel = egressLabelText(row)
  if (egressLabel) return `${label} ${egressLabel}`
  const hash = String(row.proxy_hash || '')
  if (hash && hash !== 'direct') return `${label} ${hash}`
  return label
}

export function accountEgressDigest(row: RealtimeMonitorRecord) {
  const accountWait = formatMs(metricValue(row, 'account_wait_ms'))
  const egressWait = formatMs(metricValue(row, 'egress_wait_ms'))
  return `账号 ${accountWait} / 出口 ${egressWait}`
}

export function activeEgressMeta(summary?: RealtimeMonitorSummary) {
  const items = Object.entries(summary?.active_by_egress || {})
  if (!items.length) return '代理组、默认出口、Runtime 或直连出口'
  return items
    .slice(0, 2)
    .map(([key, count]) => {
      const [source, ...rest] = key.split(':')
      const detail = rest.join(':')
      return `${proxySourceLabel(source)}${detail ? ` ${detail}` : ''} ${count}`
    })
    .join(' / ')
}

export function entryQueueText(summary?: RealtimeMonitorSummary) {
  return formatMs(maxMetricFromMap(summary?.metric_p95 || {}, ENTRY_QUEUE_METRIC_KEYS))
}

export function metricDigest(row: RealtimeMonitorRecord) {
  const parts = DIGEST_METRIC_PAIRS
    .map(([label, key]) => {
      const value = metricValue(row, key)
      return value > 0 ? { label, value, text: `${label} ${formatMs(value)}` } : null
    })
    .filter(Boolean)
    .sort((a, b) => (b?.value || 0) - (a?.value || 0))
    .map(item => item?.text || '')
  const stageElapsed = Number(row.stage_elapsed_ms || 0)
  if (String(row.status || '').toLowerCase() === 'running' && stageElapsed > 0) {
    parts.unshift(`当前阶段 ${formatMs(stageElapsed)}`)
  }
  return parts.slice(0, 4).join(' / ') || '-'
}

export function rowDurationMs(row: RealtimeMonitorRecord) {
  const value = Math.max(Number(row.duration_ms || 0), Number(row.elapsed_ms || 0))
  return Number.isFinite(value) ? Math.max(0, value) : 0
}

export function trackedDurationMs(row: RealtimeMonitorRecord) {
  const queue = metricValue(row, 'handler_queue_ms') + metricValue(row, 'stream_first_queue_ms')
  const linearStages = [
    'account_wait_ms',
    'egress_wait_ms',
    'upload_ms',
    'bootstrap_ms',
    'requirements_ms',
    'prepare_conversation_ms',
    'generation_start_ms',
    'conversation_stream_ms',
    'stream_error_ms',
    'resolve_ms',
    'download_ms',
    'retry_wait_ms',
    'response_ms',
  ].reduce((sum, key) => sum + metricValue(row, key), 0)
  const wrappedStage = Math.max(metricValue(row, 'total_ms'), metricValue(row, 'stream_ms'), linearStages)
  return queue + wrappedStage
}

export function untrackedDurationMs(row: RealtimeMonitorRecord) {
  return Math.max(0, rowDurationMs(row) - trackedDurationMs(row))
}

export function slowMetricItems(row: RealtimeMonitorRecord) {
  const items = SLOW_METRIC_PAIRS
    .map((item) => {
      const raw = metricValue(row, item.key)
      return raw > 0
        ? { ...item, raw, value: formatMs(raw), important: raw >= 10_000 }
        : null
    })
    .filter(Boolean) as MonitorSlowMetricItem[]
  const untracked = untrackedDurationMs(row)
  if (untracked >= 1000) {
    items.push({
      key: 'untracked_ms',
      label: '未标记',
      raw: untracked,
      value: formatMs(untracked),
      important: untracked >= 10_000,
    })
  }
  if (!items.length) {
    const total = rowDurationMs(row)
    if (total > 0) {
      items.push({ key: 'duration_ms', label: '总耗时', raw: total, value: formatMs(total), important: total >= 10_000 })
    }
  }
  return items
}

export function slowRowReason(row: RealtimeMonitorRecord) {
  const candidates = slowMetricItems(row)
    .filter(item => !['stream_ms', 'total_ms', 'duration_ms'].includes(item.key))
    .sort((a, b) => b.raw - a.raw)
  const top = candidates[0]
  if (!top || top.raw < 1000) return ''
  if (top.key === 'untracked_ms') {
    return `仍有 ${top.value} 没有落到具体阶段，说明这段链路还缺埋点。`
  }
  if (top.key === 'resolve_ms') {
    return `主要卡在图片结果解析/轮询，通常对应等待上游图片任务完成或轮询超时。`
  }
  if (top.key === 'conversation_stream_ms') {
    return `主要卡在上游生成中，通常是供应商生成阶段耗时。`
  }
  if (top.key === 'stream_error_ms') {
    return `主要卡在上游断流，通常是 HTTP2/SSE、代理或上游边缘节点中断。`
  }
  if (top.key === 'http_ttfb_ms' || top.key === 'http_wait_ms') {
    return `主要卡在 HTTP 首包，通常是代理出口、上游边缘节点或请求排队变慢。`
  }
  if (['http_dns_ms', 'http_tcp_ms', 'http_tls_ms'].includes(top.key)) {
    return `主要卡在 HTTP 建连阶段：${top.label} ${top.value}。`
  }
  if (top.key === 'sse_first_event_ms') {
    return `主要卡在 SSE 首事件，说明连接已建立但上游长时间没有返回首个事件。`
  }
  if (top.key === 'sse_max_gap_ms' || top.key === 'sse_last_gap_ms') {
    return `主要卡在 SSE 空窗，说明上游流中间长时间没有新事件。`
  }
  if (top.key === 'egress_wait_ms') {
    return `主要卡在等待出口，通常是代理组、默认出口、Runtime 出口或出站会话准备变慢。`
  }
  if (['upload_ms', 'bootstrap_ms', 'requirements_ms', 'prepare_conversation_ms', 'generation_start_ms'].includes(top.key)) {
    return `主要卡在上游准备阶段：${top.label} ${top.value}。`
  }
  if (top.key === 'account_wait_ms') {
    return `主要卡在等待账号，通常是可用账号不足或账号并发被占满。`
  }
  if (top.key === 'retry_wait_ms') {
    return `主要卡在重试等待，通常是轮询、TLS 或连接失败后的退避时间。`
  }
  if (top.key === 'handler_queue_ms' || top.key === 'stream_first_queue_ms') {
    return `主要卡在等待入口，通常是后端同步线程容量不足；可通过环境变量 CHATGPT2API_THREAD_TOKENS 调整。`
  }
  return `主要耗时：${top.label} ${top.value}。`
}

export function statusLabel(status: unknown) {
  const value = String(status || '').toLowerCase()
  if (value === 'success') return '成功'
  if (value === 'failed' || value === 'error' || value === 'fail') return '失败'
  if (value === 'running') return '运行中'
  return value || '-'
}

export function statusTone(status: unknown): BadgeTone {
  const value = String(status || '').toLowerCase()
  if (value === 'success') return 'success'
  if (value === 'failed' || value === 'error' || value === 'fail') return 'danger'
  if (value === 'running') return 'info'
  return 'muted'
}

export function eventMetricText(row: RealtimeMonitorEvent) {
  const parts = EVENT_METRIC_PAIRS
    .map(([label, key]) => {
      const value = Number(row[key] || 0)
      return value > 0 ? `${label} ${formatMs(value)}` : ''
    })
    .filter(Boolean)
  return parts.slice(0, 3).join(' / ') || '-'
}

function roundedMetricSignature(row: RealtimeMonitorRecord, keys: readonly string[]) {
  return keys.map(key => Math.round(metricValue(row, key))).join(',')
}

function eventMetricSignature(row: RealtimeMonitorEvent) {
  return EVENT_METRIC_PAIRS
    .map(([, key]) => Math.round(Number(row[key] || 0)))
    .join(',')
}

export function activeRowSignature(row: RealtimeMonitorRecord) {
  return [
    row.call_id,
    row.endpoint,
    row.model,
    row.stage,
    row.stage_label,
    Math.round(Number(row.elapsed_ms || 0)),
    Math.round(Number(row.stage_elapsed_ms || 0)),
    row.account_email,
    row.proxy_source,
    row.proxy_hash,
    row.egress_label,
    row.proxy_group_id,
    row.proxy_node_id,
    row.proxy_node_name,
    roundedMetricSignature(row, DIGEST_METRIC_PAIRS.map(([, key]) => key)),
  ].join('|')
}

export function recentRowSignature(row: RealtimeMonitorRecord) {
  return [
    row.call_id,
    row.ended_at,
    row.updated_at,
    row.status,
    row.model,
    Math.round(Number(row.duration_ms || 0)),
    roundedMetricSignature(row, ['handler_queue_ms', 'account_wait_ms', 'egress_wait_ms']),
  ].join('|')
}

export function slowRowSignature(row: RealtimeMonitorRecord) {
  return [
    row.call_id,
    row.ended_at,
    row.updated_at,
    row.status,
    row.model,
    row.endpoint,
    row.error,
    Math.round(Number(row.duration_ms || 0)),
    roundedMetricSignature(row, RECORD_SIGNATURE_METRIC_KEYS),
  ].join('|')
}

export function eventRowSignature(row: RealtimeMonitorEvent, index: number) {
  return [
    row.call_id,
    row.event,
    row.label,
    row.time,
    row.model,
    index,
    eventMetricSignature(row),
  ].join('|')
}

export function completedWindowText(windowInfo?: RealtimeMonitorResponse['window']) {
  if (!windowInfo) return '窗口 0 / 0'
  return `窗口 ${windowInfo.completed} / ${windowInfo.completed_capacity}`
}

export function activeStageItems(summary?: RealtimeMonitorSummary): MonitorStageCountItem[] {
  return Object.entries(summary?.active_by_stage || {})
    .filter(([, count]) => Number(count) > 0)
    .slice(0, 8)
    .map(([label, count]) => ({ label, count: Number(count) }))
}

export function buildDiagnosticGroups(
  summary: RealtimeMonitorSummary | undefined,
  threadTokens: string | number,
  completedWindowText: string,
) {
  const p95 = summary?.metric_p95 || {}
  const bottleneckValue = Number(summary?.bottleneck?.value_ms || 0)
  const localBusy = summary?.slow_counts?.local_reject_or_busy ?? 0
  const entryAccountTotal = sumMetricFromMap(p95, ENTRY_ACCOUNT_METRIC_KEYS)
  const httpConnectTotal = sumMetricFromMap(p95, ['http_dns_ms', 'http_tcp_ms', 'http_tls_ms'])
  const currentEntryQueueText = entryQueueText(summary)

  return [
    {
      key: 'overview',
      title: '实时概览',
      meta: '窗口、成功率、瓶颈',
      items: [
        { key: 'active', label: '当前并发', value: summary?.active ?? 0, meta: `线程容量 ${threadTokens}`, valueClass: 'text-foreground' },
        { key: 'completed', label: '完成窗口', value: summary?.completed ?? 0, meta: completedWindowText, valueClass: 'text-foreground' },
        { key: 'success', label: '成功率', value: `${summary?.success_rate ?? 0}%`, meta: `成功 ${summary?.success ?? 0}`, valueClass: 'text-emerald-600 dark:text-emerald-400' },
        { key: 'failed', label: '失败数', value: summary?.failed ?? 0, meta: '窗口内失败', valueClass: Number(summary?.failed || 0) > 0 ? 'text-rose-600 dark:text-rose-400' : 'text-foreground' },
        { key: 'average', label: '平均耗时', value: formatMs(summary?.avg_duration_ms), meta: '窗口均值', valueClass: 'text-sky-600 dark:text-sky-400' },
        { key: 'p95', label: 'P95 耗时', value: formatMs(summary?.p95_duration_ms), meta: '慢请求参考', valueClass: 'text-sky-600 dark:text-sky-400' },
        { key: 'bottleneck', label: '当前瓶颈', value: summary?.bottleneck?.label || '-', meta: 'P95 最大阶段', valueClass: 'text-foreground' },
        { key: 'bottleneck_ms', label: '瓶颈耗时', value: formatMs(bottleneckValue), meta: '阶段 P95', valueClass: 'text-foreground' },
      ],
    },
    {
      key: 'account',
      title: '入口、账号与出口',
      meta: '本地线程、账号池、代理出口',
      items: [
        { key: 'handler_queue_ms', label: '入口排队', value: formatMs(p95.handler_queue_ms), meta: '等待后端线程', valueClass: 'text-sky-600 dark:text-sky-400' },
        { key: 'stream_first_queue_ms', label: '首包排队', value: formatMs(p95.stream_first_queue_ms), meta: '等待流式首包', valueClass: 'text-sky-600 dark:text-sky-400' },
        { key: 'account_wait_ms', label: '账号等待', value: formatMs(p95.account_wait_ms), meta: '账号池筛选', valueClass: 'text-cyan-600 dark:text-cyan-400' },
        { key: 'egress_wait_ms', label: '出口等待', value: formatMs(p95.egress_wait_ms), meta: activeEgressMeta(summary), valueClass: 'text-teal-600 dark:text-teal-400' },
        { key: 'egress_acquire_ms', label: '出口租约', value: formatMs(p95.egress_acquire_ms), meta: '代理节点并发', valueClass: 'text-teal-600 dark:text-teal-400' },
        { key: 'entry_account_total_ms', label: '入口账号合计', value: formatMs(entryAccountTotal), meta: '入口 + 首包 + 账号 + 出口', valueClass: 'text-sky-600 dark:text-sky-400' },
        { key: 'entry_p95', label: '入口排队 P95', value: currentEntryQueueText, meta: `线程容量 ${threadTokens} · 慢 ${summary?.slow_counts?.handler_queue ?? 0}`, valueClass: 'text-sky-600 dark:text-sky-400' },
        { key: 'local_busy', label: '本地拒绝/繁忙', value: `${localBusy}`, meta: '无号 / 并发 / 策略', valueClass: 'text-foreground' },
      ],
    },
    {
      key: 'upstream_prepare',
      title: '上游准备与 HTTP',
      meta: '上传、令牌、建连、首包',
      items: [
        { key: 'upload_ms', label: '图片上传', value: formatMs(p95.upload_ms), meta: '参考图上传', valueClass: 'text-foreground' },
        { key: 'bootstrap_ms', label: '上游初始化', value: formatMs(p95.bootstrap_ms), meta: '供应商会话', valueClass: 'text-foreground' },
        { key: 'requirements_ms', label: '令牌获取', value: formatMs(p95.requirements_ms), meta: 'requirements / token', valueClass: 'text-foreground' },
        { key: 'prepare_conversation_ms', label: '会话准备', value: formatMs(p95.prepare_conversation_ms), meta: '准备图片会话', valueClass: 'text-foreground' },
        { key: 'generation_start_ms', label: '启动生成', value: formatMs(p95.generation_start_ms), meta: '提交上游请求', valueClass: 'text-foreground' },
        { key: 'http_connect_ms', label: 'HTTP 建连', value: formatMs(httpConnectTotal), meta: 'DNS + TCP + TLS', valueClass: 'text-sky-600 dark:text-sky-400' },
        { key: 'http_wait_ms', label: 'HTTP 等待', value: formatMs(p95.http_wait_ms), meta: '发出请求到首包', valueClass: 'text-sky-600 dark:text-sky-400' },
        { key: 'http_ttfb_ms', label: 'HTTP 首包', value: formatMs(p95.http_ttfb_ms), meta: '请求开始到首包', valueClass: 'text-sky-600 dark:text-sky-400' },
      ],
    },
    {
      key: 'upstream_result',
      title: '生成与结果',
      meta: '流、轮询、下载',
      items: [
        { key: 'sse_first_event_ms', label: 'SSE 首事件', value: formatMs(p95.sse_first_event_ms), meta: '首个 data 事件', valueClass: 'text-indigo-600 dark:text-indigo-400' },
        { key: 'sse_max_gap_ms', label: 'SSE 最大空窗', value: formatMs(p95.sse_max_gap_ms), meta: '两次事件最大间隔', valueClass: 'text-indigo-600 dark:text-indigo-400' },
        { key: 'conversation_stream_ms', label: '上游生成', value: formatMs(p95.conversation_stream_ms), meta: '会话流响应', valueClass: 'text-emerald-600 dark:text-emerald-400' },
        { key: 'stream_error_ms', label: '上游断流', value: formatMs(p95.stream_error_ms), meta: 'HTTP2 / SSE', valueClass: 'text-slate-600 dark:text-slate-300' },
        { key: 'resolve_ms', label: '图片解析', value: formatMs(p95.resolve_ms), meta: 'conversation / file', valueClass: 'text-emerald-600 dark:text-emerald-400' },
        { key: 'download_ms', label: '图片下载', value: formatMs(p95.download_ms), meta: '下载并返回', valueClass: 'text-foreground' },
        { key: 'stream_ms', label: '单图内部', value: formatMs(p95.stream_ms), meta: '上游到结果', valueClass: 'text-foreground' },
        { key: 'total_ms', label: '单图总耗时', value: formatMs(p95.total_ms), meta: '完整链路', valueClass: 'text-foreground' },
      ],
    },
  ] satisfies MonitorDiagnosticGroup[]
}
