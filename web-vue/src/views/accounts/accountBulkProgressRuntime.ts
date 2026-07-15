import { computed, ref } from 'vue'

import type { AccountRefreshProgress } from '@/api/accounts'

export type AccountBulkProgressKind = 'refresh' | 'mutation' | 'checkout'

type AccountBulkProgressPatch = Partial<AccountRefreshProgress> & {
  total: number
  processed?: number
}

export function useAccountBulkProgressRuntime() {
  const batchBusy = ref(false)
  const batchActionLabel = ref('')
  const showRefreshProgress = ref(false)
  const refreshProgressTitle = ref('')
  const refreshProgress = ref<AccountRefreshProgress | null>(null)
  const refreshProgressKind = ref<AccountBulkProgressKind>('refresh')
  const bulkStopRequested = ref(false)

  const refreshProgressPercent = computed(() => {
    const progress = refreshProgress.value
    const total = Math.max(0, Number(progress?.total || 0))
    if (total <= 0) return 0
    return Math.min(100, Math.round((Math.max(0, Number(progress?.processed || 0)) / total) * 100))
  })

  const refreshProgressMetricLabel = computed(() => (
    refreshProgressKind.value === 'refresh' ? '图片总额度' : '处理账号'
  ))

  const refreshProgressMetricValue = computed(() => {
    const progress = refreshProgress.value
    if (refreshProgressKind.value === 'refresh') return progress?.total_quota ?? '-'
    return `${progress?.processed || 0} 个`
  })

  const refreshProgressStatusText = computed(() => {
    const progress = refreshProgress.value
    if (progress?.error) return '失败'
    if (progress?.done) return bulkStopRequested.value ? '已停止' : '已完成'
    if (bulkStopRequested.value) return '停止中'
    if (refreshProgressKind.value === 'refresh') return '刷新中'
    if (refreshProgressKind.value === 'checkout') return '提链中'
    return '处理中'
  })

  const canStopRefreshProgress = computed(() => (
    showRefreshProgress.value && batchBusy.value && !refreshProgress.value?.done
  ))

  function start(title: string, total: number, kind: AccountBulkProgressKind) {
    bulkStopRequested.value = false
    batchBusy.value = true
    batchActionLabel.value = title
    showRefreshProgress.value = true
    refreshProgressTitle.value = title
    refreshProgressKind.value = kind
    refreshProgress.value = {
      total,
      processed: 0,
      done: false,
      error: null,
      total_quota: kind === 'refresh' ? 0 : undefined,
      result: null,
    }
  }

  function end() {
    batchBusy.value = false
    batchActionLabel.value = ''
  }

  function update(patch: AccountBulkProgressPatch) {
    refreshProgress.value = {
      ...(refreshProgress.value || { total: patch.total, processed: 0, done: false }),
      ...patch,
      done: Boolean(patch.done),
    }
  }

  function finish(patch: AccountBulkProgressPatch) {
    refreshProgress.value = {
      ...(refreshProgress.value || { total: patch.total, processed: patch.processed || 0, done: false }),
      ...patch,
      done: true,
    }
  }

  function fail(total: number, processed: number, error: string) {
    refreshProgress.value = {
      ...(refreshProgress.value || { total, processed, done: false }),
      total,
      processed,
      done: true,
      error,
    }
  }

  function requestStop() {
    if (!canStopRefreshProgress.value) return false
    bulkStopRequested.value = true
    return true
  }

  function close() {
    if (!refreshProgress.value?.done && batchBusy.value) return false
    showRefreshProgress.value = false
    return true
  }

  return {
    batchBusy,
    batchActionLabel,
    showRefreshProgress,
    refreshProgressTitle,
    refreshProgress,
    refreshProgressPercent,
    refreshProgressMetricLabel,
    refreshProgressMetricValue,
    refreshProgressStatusText,
    canStopRefreshProgress,
    bulkStopRequested,
    start,
    end,
    update,
    finish,
    fail,
    requestStop,
    close,
  }
}
