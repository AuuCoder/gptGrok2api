import apiClient from './client'

export type MonitorMetricMap = Record<string, number>

export interface RealtimeMonitorImage {
  index?: number
  total?: number
  stage?: string
  stage_label?: string
  status?: string
  returned_result?: boolean
  returned_message?: boolean
  metrics?: MonitorMetricMap
  proxy_source?: string
  proxy_hash?: string
  egress_key?: string
  egress_label?: string
  proxy_group_id?: string
  proxy_node_id?: string
  proxy_node_name?: string
  image_egress_limit?: number
  has_proxy?: boolean
  egress_mode?: string
  local_reason?: string
  error?: string
  raw_error?: string
  upstream_error?: string
  upstream_message?: string
}

export interface RealtimeMonitorRecord {
  call_id: string
  endpoint?: string
  model?: string
  summary?: string
  role?: string
  key_name?: string
  status?: string
  stage?: string
  stage_label?: string
  started_at?: string
  ended_at?: string
  updated_at?: string
  elapsed_ms?: number
  stage_elapsed_ms?: number
  duration_ms?: number
  metrics?: MonitorMetricMap
  perf?: MonitorMetricMap
  images?: Record<string, RealtimeMonitorImage>
  account_email?: string
  conversation_id?: string
  error?: string
  raw_error?: string
  upstream_error?: string
  upstream_message?: string
  url_count?: number
  proxy_source?: string
  proxy_hash?: string
  egress_key?: string
  egress_label?: string
  proxy_group_id?: string
  proxy_node_id?: string
  proxy_node_name?: string
  image_egress_limit?: number
  has_proxy?: boolean
  egress_mode?: string
  local_reason?: string
}

export interface RealtimeMonitorSummary {
  active: number
  completed: number
  success: number
  failed: number
  success_rate: number
  avg_duration_ms: number
  p95_duration_ms: number
  metric_p95: MonitorMetricMap
  slow_counts: {
    handler_queue: number
    stream_first_queue: number
    account_wait: number
    egress_wait: number
    total_over_120s: number
    local_reject_or_busy: number
  }
  bottleneck: {
    key: string
    label: string
    value_ms: number
  }
  by_model: Record<string, number>
  active_by_model: Record<string, number>
  active_by_egress?: Record<string, number>
  active_by_stage?: Record<string, number>
}

export interface RealtimeMonitorEvent {
  time: string
  call_id: string
  event: string
  label: string
  model?: string
  index?: number
  total?: number
  status?: string
  [key: string]: unknown
}

export interface RealtimeMonitorResponse {
  updated_at: string
  threadpool: {
    tokens: number
    previous_tokens: number
  }
  window: {
    completed: number
    completed_capacity: number
    events: number
    event_capacity: number
  }
  summary: RealtimeMonitorSummary
  active: RealtimeMonitorRecord[]
  recent: RealtimeMonitorRecord[]
  slow: RealtimeMonitorRecord[]
  events: RealtimeMonitorEvent[]
  metric_labels: Record<string, string>
}

export const monitorApi = {
  realtime() {
    return apiClient.get<never, RealtimeMonitorResponse>('/api/monitor/realtime')
  },
}
