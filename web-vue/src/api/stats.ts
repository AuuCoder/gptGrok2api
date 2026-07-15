import apiClient from './client'
import type { AdminStats, DashboardResponse } from '@/types/api'

function seriesFromRecord(record: Record<string, number> | undefined) {
  return Object.fromEntries(
    Object.entries(record || {})
      .filter(([, value]) => Number(value) > 0)
      .map(([key, value]) => [key, [Number(value)]])
  )
}

function fallbackTrend(logs: DashboardResponse['logs']) {
  const successCount = Number(logs.success || 0)
  const failedCount = Number(logs.failed || 0)
  const totalRequests = Math.max(Number(logs.total || 0), successCount + failedCount)
  const rateLimited = Number(logs.by_error_code?.rate_limited || logs.by_error_code?.rate_limit || 0)
  return {
    labels: ['当前'],
    total_requests: [totalRequests],
    success_requests: [successCount],
    failed_requests: [Math.max(failedCount - rateLimited, 0)],
    rate_limited_requests: [rateLimited],
    model_requests: seriesFromRecord(logs.by_model || {}),
    model_ttfb_times: {},
    model_total_times: {},
  }
}

function adaptDashboardToStats(dashboard: DashboardResponse): AdminStats {
  const accounts = dashboard.accounts || {}
  const logs = dashboard.logs || {
    total: 0,
    success: 0,
    failed: 0,
    by_endpoint: {},
    by_model: {},
    by_status: {},
    by_error_code: {},
    recent_failures: [],
  }
  const active = Number(accounts.active || 0)
  const limited = Number(accounts.limited || 0)
  const abnormal = Number(accounts.abnormal || 0)
  const disabled = Number(accounts.disabled || 0)
  const total = Number(accounts.total || 0)
  const failedAccounts = abnormal + disabled
  const idleAccounts = Math.max(total - active - limited - failedAccounts, 0)
  const successCount = Number(logs.success || 0)
  const failedCount = Number(logs.failed || 0)
  const trend = logs.trend || fallbackTrend(logs)

  return {
    total_accounts: total,
    active_accounts: active,
    abnormal_accounts: abnormal,
    disabled_accounts: disabled,
    failed_accounts: failedAccounts,
    rate_limited_accounts: limited,
    idle_accounts: idleAccounts,
    total_quota: Number(accounts.total_quota || 0),
    unlimited_quota_count: Number(accounts.unlimited_quota_count || 0),
    unknown_quota_count: Number(accounts.unknown_quota_count || 0),
    account_providers: accounts.providers || {},
    success_count: successCount,
    failed_count: failedCount,
    recent_failures: Array.isArray(logs.recent_failures) ? logs.recent_failures : [],
    trend,
  }
}

export const statsApi = {
  async overview(timeRange: string = '24h') {
    const dashboard = await apiClient.get<never, DashboardResponse>('/api/dashboard', {
      params: {
        time_range: timeRange,
      },
    })
    return adaptDashboardToStats(dashboard)
  },
}
