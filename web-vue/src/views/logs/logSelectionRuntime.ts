import { computed, reactive, ref, type Ref } from 'vue'
import type { SystemLogRow } from '@/api/logs'
import { errorMessage } from '@/lib/errorMessage'

export type LogSelectionRuntimeInput = {
  visibleLogs: Ref<SystemLogRow[]>
  selectedLog: Ref<SystemLogRow | null>
  deleteLogs: (ids: string[]) => Promise<{ removed: number }>
  refreshLogs: () => Promise<void>
  notifySuccess: (message: string) => void
  notifyError: (message: string) => void
}

export function useLogSelectionRuntime(input: LogSelectionRuntimeInput) {
  const selectedLogIds = ref<string[]>([])
  const deleteTarget = ref<SystemLogRow | null>(null)
  const deleteSelectedOpen = ref(false)
  const isDeleting = ref(false)
  const operationProgress = reactive({
    open: false,
    title: '',
    subtitle: '',
    total: 0,
    current: 0,
    statusLabel: '已处理',
    message: '',
    error: '',
    busy: false,
  })

  const currentLogIdSet = computed(() => new Set(input.visibleLogs.value.map((item) => item.id).filter(Boolean)))
  const selectedDeletableLogIds = computed(() => (
    Array.from(new Set(selectedLogIds.value)).filter((id) => currentLogIdSet.value.has(id))
  ))
  const selectedLogIdSet = computed(() => new Set(selectedDeletableLogIds.value))
  const selectedLogCount = computed(() => selectedDeletableLogIds.value.length)
  const allVisibleLogsSelected = computed(() => {
    if (input.visibleLogs.value.length === 0) return false
    return input.visibleLogs.value.every((item) => selectedLogIdSet.value.has(item.id))
  })

  function pruneSelectionToVisible() {
    selectedLogIds.value = selectedLogIds.value.filter((id) => currentLogIdSet.value.has(id))
  }

  function isLogSelected(id: string): boolean {
    return selectedLogIdSet.value.has(id)
  }

  function toggleLogSelection(id: string, checked?: boolean) {
    const next = new Set(selectedLogIds.value)
    const shouldSelect = typeof checked === 'boolean' ? checked : !next.has(id)
    if (shouldSelect) next.add(id)
    else next.delete(id)
    selectedLogIds.value = Array.from(next)
  }

  function toggleSelectAllVisibleLogs(checked?: boolean) {
    const next = new Set(selectedLogIds.value)
    const shouldSelect = typeof checked === 'boolean' ? checked : !allVisibleLogsSelected.value
    input.visibleLogs.value.forEach((item) => {
      if (shouldSelect) next.add(item.id)
      else next.delete(item.id)
    })
    selectedLogIds.value = Array.from(next)
  }

  function clearLogSelection() {
    selectedLogIds.value = []
  }

  function removeSelectedLogIds(ids: readonly string[]) {
    const deleted = new Set(ids)
    selectedLogIds.value = selectedLogIds.value.filter((id) => !deleted.has(id))
  }

  function requestDeleteLog(item: SystemLogRow) {
    deleteTarget.value = item
  }

  function requestDeleteSelectedLogs() {
    if (selectedLogCount.value === 0) return
    deleteSelectedOpen.value = true
  }

  function resetOperationProgress(payload: {
    title: string
    subtitle: string
    total: number
    message: string
  }) {
    operationProgress.open = true
    operationProgress.title = payload.title
    operationProgress.subtitle = payload.subtitle
    operationProgress.total = payload.total
    operationProgress.current = 0
    operationProgress.statusLabel = '已提交'
    operationProgress.message = payload.message
    operationProgress.error = ''
    operationProgress.busy = true
  }

  async function deleteLog() {
    const item = deleteTarget.value
    if (!item) return
    deleteTarget.value = null
    isDeleting.value = true
    resetOperationProgress({
      title: '删除日志',
      subtitle: item.time || item.id,
      total: 1,
      message: '正在提交删除请求...',
    })
    try {
      await input.deleteLogs([item.id])
      operationProgress.current = 1
      operationProgress.statusLabel = '已处理'
      operationProgress.message = '删除完成，正在刷新列表...'
      if (input.selectedLog.value?.id === item.id) input.selectedLog.value = null
      removeSelectedLogIds([item.id])
      input.notifySuccess('日志已删除')
      await input.refreshLogs()
      operationProgress.message = '日志已删除'
    } catch (error) {
      operationProgress.error = errorMessage(error, '删除失败')
      input.notifyError(operationProgress.error)
    } finally {
      isDeleting.value = false
      operationProgress.busy = false
    }
  }

  async function deleteSelectedLogs() {
    const ids = selectedDeletableLogIds.value
    if (ids.length === 0) {
      deleteSelectedOpen.value = false
      return
    }
    deleteSelectedOpen.value = false
    isDeleting.value = true
    resetOperationProgress({
      title: '批量删除日志',
      subtitle: `已选择 ${ids.length} 条`,
      total: ids.length,
      message: '正在提交批量删除请求...',
    })
    try {
      const result = await input.deleteLogs(ids)
      const removed = Number(result.removed ?? ids.length)
      operationProgress.current = removed
      operationProgress.statusLabel = '已处理'
      operationProgress.message = '删除完成，正在刷新列表...'
      if (input.selectedLog.value && ids.includes(input.selectedLog.value.id)) input.selectedLog.value = null
      clearLogSelection()
      input.notifySuccess(`已删除 ${removed} 条日志`)
      await input.refreshLogs()
      operationProgress.message = `已删除 ${removed} 条日志`
    } catch (error) {
      operationProgress.error = errorMessage(error, '删除失败')
      input.notifyError(operationProgress.error)
    } finally {
      isDeleting.value = false
      operationProgress.busy = false
    }
  }

  return {
    selectedLogIds,
    deleteTarget,
    deleteSelectedOpen,
    isDeleting,
    operationProgress,
    selectedDeletableLogIds,
    selectedLogIdSet,
    selectedLogCount,
    allVisibleLogsSelected,
    pruneSelectionToVisible,
    isLogSelected,
    toggleLogSelection,
    toggleSelectAllVisibleLogs,
    clearLogSelection,
    requestDeleteLog,
    requestDeleteSelectedLogs,
    deleteLog,
    deleteSelectedLogs,
  }
}
