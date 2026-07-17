import { computed, ref, watch } from 'vue'

import {
  grokAccountsApi,
  type GrokAccount,
  type GrokAccountExportFormat,
  type GrokAccountsSummary,
  type GrokAccountStatusFilter,
  type GrokAccountVerificationStatus,
} from '@/api/grokAccounts'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { usePageDebouncedAction, usePagedQuery } from '@/composables/usePageQuery'
import { usePageRuntime } from '@/composables/usePageRuntime'
import { useToast } from '@/composables/useToast'
import { saveBlob } from '@/lib/downloads'
import { errorMessage } from '@/lib/errorMessage'

export type GrokAccountsViewMode = 'list' | 'cards'
export type GrokAccountBulkAction = 'sync' | 'refresh' | 'disable' | 'enable' | 'delete'

const DEFAULT_PAGE_SIZE = 20
const LIST_REQUEST_KEY = 'grok-accounts:list'
const SEARCH_TIMER_KEY = 'grok-accounts:search'

export const grokAccountPageSizeOptions = [20, 50, 100] as const

export const grokAccountStatusFilterOptions = [
  { label: '全部状态', value: 'all' },
  { label: '可用', value: 'active' },
  { label: '待获取登录态', value: 'pending_sso' },
  { label: '提交中', value: 'submitting' },
  { label: '待提交', value: 'pending_submit' },
  { label: '提交失败', value: 'submission_failed' },
  { label: '提交结果未知', value: 'submission_unknown' },
  { label: '提交待确认', value: 'submission_unconfirmed' },
  { label: '运行正常', value: 'normal' },
  { label: '运行限流', value: 'limited' },
  { label: '刷新失败', value: 'refresh_failed' },
  { label: '运行异常', value: 'abnormal' },
  { label: '运行禁用', value: 'disabled' },
] as const satisfies ReadonlyArray<{ label: string; value: GrokAccountStatusFilter }>

function exportFilename(format: GrokAccountExportFormat) {
  const stamp = new Date().toISOString().slice(0, 19).replaceAll(':', '-')
  return `grok-accounts-${stamp}.${format}`
}

export function useGrokAccountsPage() {
  const loading = ref(false)
  const hasLoadedOnce = ref(false)
  const keyword = ref('')
  const statusFilter = ref<GrokAccountStatusFilter>('all')
  const accounts = ref<GrokAccount[]>([])
  const accountAllTotal = ref(0)
  const summary = ref<GrokAccountsSummary>({})
  const runtimeAvailable = ref(false)
  const runtimeError = ref('')
  const pageSize = ref(DEFAULT_PAGE_SIZE)
  const viewMode = ref<GrokAccountsViewMode>('list')
  const selectedIds = ref<string[]>([])
  const batchBusy = ref(false)
  const batchActionLabel = ref('')
  const syncingAccountId = ref('')
  const refreshingAccountId = ref('')
  const testingAccountId = ref('')
  const togglingAccountId = ref('')
  const removingAccountId = ref('')
  const exportBusy = ref(false)

  const toast = useToast()
  const confirmDialog = useConfirmDialog()
  const pageRuntime = usePageRuntime('grok-accounts')

  const accountListQuery = usePagedQuery({
    runtime: pageRuntime,
    key: LIST_REQUEST_KEY,
    pageSize,
    loading,
    errorMessage: '加载 Grok 账号失败',
    fetch: ({ page, pageSize: size }) => grokAccountsApi.list({
      page,
      page_size: size,
      keyword: keyword.value.trim() || undefined,
      status: statusFilter.value === 'all' ? undefined : statusFilter.value,
    }),
    resolvePage: (response) => response.page,
    resolvePageCount: (response) => {
      const total = Math.max(0, Number(response.total) || 0)
      const size = Math.max(1, Number(response.page_size) || pageSize.value)
      return Math.max(1, Math.ceil(total / size))
    },
    resolveTotal: (response) => response.total,
    apply: (response) => {
      accounts.value = Array.isArray(response.items) ? response.items : []
      accountAllTotal.value = Math.max(0, Number(response.all_total) || 0)
      summary.value = response.summary && typeof response.summary === 'object' ? response.summary : {}
      runtimeAvailable.value = Boolean(response.runtime_available)
      runtimeError.value = String(response.runtime_error || '').trim()
      const currentIds = new Set(accounts.value.map((item) => item.id))
      selectedIds.value = selectedIds.value.filter((id) => currentIds.has(id))
    },
    onError: (message) => {
      toast.error(message, '加载失败')
    },
    onSettled: (latest) => {
      if (latest) hasLoadedOnce.value = true
    },
  })

  const accountListTotal = accountListQuery.total
  const currentPage = accountListQuery.currentPage
  const pageCount = accountListQuery.pageCount
  const paginationSummary = accountListQuery.paginationSummary
  const pageSizeOptions = grokAccountPageSizeOptions
  const statusFilterOptions = grokAccountStatusFilterOptions
  const hasAccounts = computed(() => accountListTotal.value > 0)
  const selectedSet = computed(() => new Set(selectedIds.value))
  const selectedCount = computed(() => selectedIds.value.length)
  const allVisibleSelected = computed(() => (
    accounts.value.length > 0 && accounts.value.every((item) => selectedSet.value.has(item.id))
  ))

  const searchDebounce = usePageDebouncedAction({
    runtime: pageRuntime,
    key: SEARCH_TIMER_KEY,
    delayMs: 250,
    action: () => accountListQuery.resetAndLoad(),
  })

  async function loadData(options: { silentErrorToast?: boolean; silentLoading?: boolean } = {}) {
    await accountListQuery.load({
      silentError: options.silentErrorToast,
      silentLoading: options.silentLoading,
    })
  }

  function setViewMode(mode: GrokAccountsViewMode) {
    viewMode.value = mode
  }

  function uniqueIds(ids: readonly string[]) {
    return Array.from(new Set(ids.map((id) => String(id || '').trim()).filter(Boolean)))
  }

  function isSelected(accountId: string) {
    return selectedSet.value.has(accountId)
  }

  function toggleSelect(accountId: string, checked?: unknown) {
    const next = new Set(selectedIds.value)
    const shouldSelect = typeof checked === 'boolean' ? checked : !next.has(accountId)
    if (shouldSelect) next.add(accountId)
    else next.delete(accountId)
    selectedIds.value = Array.from(next)
  }

  function toggleSelectAllVisible(checked?: unknown) {
    const next = new Set(selectedIds.value)
    const shouldSelect = typeof checked === 'boolean' ? checked : !allVisibleSelected.value
    for (const item of accounts.value) {
      if (shouldSelect) next.add(item.id)
      else next.delete(item.id)
    }
    selectedIds.value = Array.from(next)
  }

  function clearSelection() {
    selectedIds.value = []
  }

  function beginAction(ids: string[], label: string, singleState: { value: string }) {
    if (ids.length === 1) singleState.value = ids[0]
    batchBusy.value = true
    batchActionLabel.value = label
  }

  function endAction(singleState: { value: string }) {
    singleState.value = ''
    batchBusy.value = false
    batchActionLabel.value = ''
  }

  async function syncAccounts(accountIds: readonly string[]) {
    if (batchBusy.value) return false
    const ids = uniqueIds(accountIds)
    if (!ids.length) return false
    const confirmed = await confirmDialog.ask({
      title: '加入 Grok 运行池',
      message: `即将把 ${ids.length} 个 Grok 注册账号加入内置 Grok 运行池，并按当前配置验证登录态。是否继续？`,
      confirmText: '开始加入',
      cancelText: '取消',
    })
    if (!confirmed) return false

    beginAction(ids, '正在加入 Grok 运行池...', syncingAccountId)
    try {
      const result = await grokAccountsApi.sync(ids)
      const ok = Number(result.summary?.ok || 0)
      const fail = Number(result.summary?.fail || 0)
      const detail = String(result.error || result.results?.find((item) => item.error)?.error || '').trim()
      if (fail > 0) toast.warning(`加入完成：成功 ${ok}，失败 ${fail}${detail ? `；${detail}` : ''}`)
      else toast.success(`已加入 ${ok} 个 Grok 账号`)
      await loadData({ silentErrorToast: true })
      return fail === 0
    } catch (error) {
      toast.error(`加入失败：${errorMessage(error)}`)
      return false
    } finally {
      endAction(syncingAccountId)
    }
  }

  async function refreshRuntime(accountIds: readonly string[]) {
    if (batchBusy.value) return false
    const ids = uniqueIds(accountIds)
    if (!ids.length) return false
    if (!runtimeAvailable.value) {
      toast.warning(runtimeError.value || 'Grok 运行时当前不可用')
      return false
    }
    const confirmed = await confirmDialog.ask({
      title: '刷新 Grok 状态和额度',
      message: `即将通过 Grok 运行时刷新 ${ids.length} 个账号的运行状态和额度。是否继续？`,
      confirmText: '开始刷新',
      cancelText: '取消',
    })
    if (!confirmed) return false

    beginAction(ids, '正在刷新状态和额度...', refreshingAccountId)
    try {
      const result = await grokAccountsApi.refreshRuntime(ids)
      const ok = Number(result.summary?.ok || 0)
      const fail = Number(result.summary?.fail || 0)
      const detail = String(result.error || result.results?.find((item) => item.error)?.error || '').trim()
      if (fail > 0) toast.warning(`刷新完成：成功 ${ok}，失败 ${fail}${detail ? `；${detail}` : ''}`)
      else toast.success(`已刷新 ${ok} 个 Grok 账号`)
      await loadData({ silentErrorToast: true })
      return fail === 0
    } catch (error) {
      toast.error(`刷新失败：${errorMessage(error)}`)
      return false
    } finally {
      endAction(refreshingAccountId)
    }
  }

  async function testAccountValidity(account: GrokAccount) {
    if (batchBusy.value) return false
    const id = String(account.id || '').trim()
    if (!id) return false
    if (!account.has_sso) {
      toast.warning('该账号没有可测试的 SSO 登录态')
      return false
    }

    beginAction([id], '正在测试登录态...', testingAccountId)
    try {
      const result = await grokAccountsApi.verifyRuntime([id])
      const verification = result.results?.find((item) => item.id === id)
      const status: GrokAccountVerificationStatus = verification?.status || 'unknown'
      const detail = String(verification?.error || result.error || '').trim()
      if (status === 'valid') {
        toast.success(`Grok 账号登录态有效${detail ? `：${detail}` : ''}`)
      } else if (status === 'invalid') {
        toast.error(`Grok 账号登录态已失效${detail ? `：${detail}` : ''}`)
      } else {
        toast.warning(`无法确认 Grok 账号登录态${detail ? `：${detail}` : ''}`)
      }
      return status === 'valid'
    } catch (error) {
      toast.warning(`无法确认 Grok 账号登录态：${errorMessage(error)}`)
      return false
    } finally {
      await loadData({ silentErrorToast: true })
      endAction(testingAccountId)
    }
  }

  async function setRuntimeDisabled(accountIds: readonly string[], disabled: boolean) {
    if (batchBusy.value) return false
    const ids = uniqueIds(accountIds)
    if (!ids.length) return false
    if (!runtimeAvailable.value) {
      toast.warning(runtimeError.value || 'Grok 运行时当前不可用')
      return false
    }
    const action = disabled ? '禁用' : '恢复'
    const confirmed = await confirmDialog.ask({
      title: `${action} Grok 账号`,
      message: `即将在 Grok 运行时中${action} ${ids.length} 个账号。${disabled ? '禁用后不会参与请求分配。' : '恢复后将重新参与请求分配。'}是否继续？`,
      confirmText: action,
      cancelText: '取消',
    })
    if (!confirmed) return false

    beginAction(ids, `正在${action}账号...`, togglingAccountId)
    try {
      const result = await grokAccountsApi.setRuntimeDisabled(ids, disabled)
      const ok = Number(result.summary?.ok || 0)
      const fail = Number(result.summary?.fail || 0)
      if (fail > 0) toast.warning(`${action}完成：成功 ${ok}，失败 ${fail}${result.error ? `；${result.error}` : ''}`)
      else toast.success(`已${action} ${ok} 个 Grok 账号`)
      await loadData({ silentErrorToast: true })
      return fail === 0
    } catch (error) {
      toast.error(`${action}失败：${errorMessage(error)}`)
      return false
    } finally {
      endAction(togglingAccountId)
    }
  }

  async function removeAccounts(accountIds: readonly string[]) {
    if (batchBusy.value) return false
    const ids = uniqueIds(accountIds)
    if (!ids.length) return false
    const rows = accounts.value.filter((item) => ids.includes(item.id))
    const syncedIds = rows.filter((item) => item.sync_state === 'synced').map((item) => item.id)
    const localOnlyIds = ids.filter((id) => !syncedIds.includes(id))
    const confirmed = await confirmDialog.ask({
      title: ids.length === 1 ? '删除 Grok 账号' : '批量删除 Grok 账号',
      message: `即将删除 ${ids.length} 个 Grok 账号，本地保存的密码和 SSO 登录态会一并移除${syncedIds.length ? '；已加入账号也会从 Grok 运行时删除' : ''}。此操作不可恢复，是否继续？`,
      confirmText: '确认删除',
      cancelText: '取消',
    })
    if (!confirmed) return false

    beginAction(ids, '正在删除账号...', removingAccountId)
    try {
      let removed = 0
      if (syncedIds.length) {
        removed += Number((await grokAccountsApi.remove(syncedIds, true)).removed || 0)
      }
      if (localOnlyIds.length) {
        removed += Number((await grokAccountsApi.remove(localOnlyIds, false)).removed || 0)
      }
      toast.success(`已删除 ${removed} 个 Grok 账号`)
      clearSelection()
      await loadData({ silentErrorToast: true })
      return true
    } catch (error) {
      toast.error(`删除失败：${errorMessage(error)}`)
      await loadData({ silentErrorToast: true })
      return false
    } finally {
      endAction(removingAccountId)
    }
  }

  async function removeAccount(account: GrokAccount | string) {
    const id = typeof account === 'string' ? account : account.id
    return removeAccounts([id])
  }

  async function runBulkAction(action: GrokAccountBulkAction) {
    const ids = [...selectedIds.value]
    if (!ids.length) {
      toast.warning('请先选择 Grok 账号')
      return
    }
    const selectedRows = accounts.value.filter((item) => ids.includes(item.id))
    const readyIds = selectedRows.filter((item) => item.has_sso).map((item) => item.id)
    const syncedIds = selectedRows.filter((item) => item.sync_state === 'synced').map((item) => item.id)
    if (action === 'sync') {
      if (!readyIds.length) toast.warning('选中账号都没有可加入运行池的 SSO 登录态')
      else await syncAccounts(readyIds)
    }
    if (action === 'refresh') {
      if (!syncedIds.length) toast.warning('选中账号尚未加入 Grok 运行池')
      else await refreshRuntime(syncedIds)
    }
    if (action === 'disable') {
      if (!syncedIds.length) toast.warning('选中账号尚未加入 Grok 运行池')
      else await setRuntimeDisabled(syncedIds, true)
    }
    if (action === 'enable') {
      if (!syncedIds.length) toast.warning('选中账号尚未加入 Grok 运行池')
      else await setRuntimeDisabled(syncedIds, false)
    }
    if (action === 'delete') await removeAccounts(ids)
  }

  async function exportAccounts(format: GrokAccountExportFormat = 'json') {
    if (exportBusy.value) return
    if (!accountAllTotal.value) {
      toast.warning('暂无可导出的 Grok 账号')
      return
    }

    const confirmed = await confirmDialog.ask({
      title: `导出 Grok 账号 ${format.toUpperCase()}`,
      message: `即将导出全部 ${accountAllTotal.value} 个 Grok 账号。文件包含密码和 SSO 登录态，请只在可信环境保存。`,
      confirmText: '确认导出',
      cancelText: '取消',
    })
    if (!confirmed) return

    exportBusy.value = true
    try {
      const blob = await grokAccountsApi.export(format)
      if (!blob.size) throw new Error('导出文件为空')
      saveBlob(blob, exportFilename(format))
      toast.success(`Grok 账号 ${format.toUpperCase()} 已导出`)
    } catch (error) {
      toast.error(`导出失败：${errorMessage(error)}`)
    } finally {
      exportBusy.value = false
    }
  }

  watch(keyword, () => {
    clearSelection()
    searchDebounce.schedule()
  })

  watch([statusFilter, pageSize], () => {
    clearSelection()
    if (!pageRuntime.isActive.value) return
    accountListQuery.resetAndLoad()
  })

  watch(currentPage, () => {
    clearSelection()
  })

  pageRuntime.onActivate(() => {
    void loadData()
  })

  pageRuntime.onShow(() => {
    void loadData({ silentErrorToast: true, silentLoading: true })
  })

  return {
    loading,
    hasLoadedOnce,
    keyword,
    statusFilter,
    statusFilterOptions,
    accounts,
    hasAccounts,
    summary,
    runtimeAvailable,
    runtimeError,
    accountListTotal,
    accountAllTotal,
    currentPage,
    pageCount,
    paginationSummary,
    pageSize,
    pageSizeOptions,
    viewMode,
    setViewMode,
    loadData,
    selectedIds,
    selectedCount,
    allVisibleSelected,
    isSelected,
    toggleSelect,
    toggleSelectAllVisible,
    clearSelection,
    batchBusy,
    batchActionLabel,
    syncingAccountId,
    syncAccounts,
    refreshingAccountId,
    refreshRuntime,
    testingAccountId,
    testAccountValidity,
    togglingAccountId,
    setRuntimeDisabled,
    removingAccountId,
    removeAccount,
    removeAccounts,
    runBulkAction,
    exportBusy,
    exportAccounts,
  }
}
