import type { Ref } from 'vue'

import { accountsApi, type Account, type AccountGroup } from '@/api/accounts'
import type { ProxyGroup } from '@/api/proxy'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import type { useAccountBulkProgressRuntime } from './accountBulkProgressRuntime'

export type AccountBulkAction = 'refresh' | 'reset' | 'enable' | 'disable' | 'delete'

type AccountSelectionAdapter = {
  selectedIds: Ref<string[]>
  clearSelection: () => void
  removeSelectedIds: (ids: readonly string[]) => void
}

type AccountBulkMutationResponse = {
  success_count?: number
  updated?: number
  removed?: number
  errors?: unknown[]
}

type AccountBulkMutationSummary = {
  success_count: number
  errors: string[]
  stopped: boolean
  processed: number
  processed_ids: string[]
}

type AccountBulkActionsRuntimeOptions = {
  bulkProgress: ReturnType<typeof useAccountBulkProgressRuntime>
  accountSelection: AccountSelectionAdapter
  accounts: Ref<Account[]>
  accountGroups: Ref<AccountGroup[]>
  proxyGroups: Ref<ProxyGroup[]>
  selectedBindGroupId: Ref<string>
  accountAllTotal: Ref<number>
  accountListTotal: Ref<number>
  refreshBatchSize: number
  normalizeErrorMessage: (error: unknown) => string
  setError: (prefix: string, error: unknown, notify?: boolean) => void
  loadData: (options?: { silentErrorToast?: boolean }) => Promise<void>
  applyAccountGroupsPayload: (response: { groups?: AccountGroup[]; proxy_groups?: ProxyGroup[] }) => void
}

function uniqueIds(ids: readonly string[]) {
  return Array.from(new Set(ids.map((id) => String(id || '').trim()).filter(Boolean)))
}

function refreshErrorText(entry: unknown) {
  if (typeof entry === 'string') return entry.trim()
  if (!entry || typeof entry !== 'object') return ''
  const source = entry as { token?: unknown; error?: unknown }
  return [source.token, source.error]
    .map((value) => String(value || '').trim())
    .filter(Boolean)
    .join(': ')
}

function normalizeErrorList(errors: unknown) {
  if (!Array.isArray(errors)) return []
  return errors
    .map(refreshErrorText)
    .filter(Boolean)
}

function bulkActionMeta(action: AccountBulkAction) {
  return {
    refresh: { title: '批量刷新账号信息', confirmText: '确认刷新', successText: '批量刷新完成' },
    reset: { title: '批量重置账号状态', confirmText: '确认重置', successText: '批量重置完成' },
    enable: { title: '批量启用账号', confirmText: '确认启用', successText: '批量启用完成' },
    disable: { title: '批量禁用账号', confirmText: '确认禁用', successText: '批量禁用完成' },
    delete: { title: '批量删除账号', confirmText: '确认删除', successText: '批量删除完成' },
  }[action]
}

export function useAccountBulkActionsRuntime(options: AccountBulkActionsRuntimeOptions) {
  const toast = useToast()
  const confirmDialog = useConfirmDialog()

  async function refreshAccountsWithProgress(accountIds: readonly string[], title: string) {
    const targetIds = uniqueIds(accountIds)
    if (!targetIds.length) {
      toast.warning('没有可刷新的账号')
      return
    }

    const confirmed = await confirmDialog.ask({
      title,
      message: `即将按每批 ${options.refreshBatchSize} 个刷新 ${targetIds.length} 个账号的信息和额度，是否继续？`,
      confirmText: '开始刷新',
      cancelText: '取消',
    })
    if (!confirmed) return

    options.bulkProgress.start(title, targetIds.length, 'refresh')
    let processedOffset = 0
    let failedCount = 0
    const errors: string[] = []

    try {
      for (let index = 0; index < targetIds.length; index += options.refreshBatchSize) {
        if (options.bulkProgress.bulkStopRequested.value) break
        const batch = targetIds.slice(index, index + options.refreshBatchSize)
        const result = await accountsApi.refreshAccountsWithProgress(batch, (progress) => {
          options.bulkProgress.update({
            ...progress,
            total: targetIds.length,
            processed: Math.min(targetIds.length, processedOffset + Number(progress.processed || 0)),
            done: false,
          })
        })

        const batchProgress = result.progress
        const batchErrors = normalizeErrorList(batchProgress?.result?.errors)
        failedCount += batchErrors.length
        errors.push(...batchErrors)
        processedOffset += batch.length
        options.bulkProgress.update({
          ...(batchProgress || {}),
          total: targetIds.length,
          processed: Math.min(targetIds.length, processedOffset),
          done: processedOffset >= targetIds.length,
        })
        if (options.bulkProgress.bulkStopRequested.value) break
      }

      await options.loadData({ silentErrorToast: true })
      const stopped = options.bulkProgress.bulkStopRequested.value && processedOffset < targetIds.length
      options.bulkProgress.finish({
        total: targetIds.length,
        processed: stopped ? Math.min(targetIds.length, processedOffset) : targetIds.length,
      })
      if (stopped) {
        toast.warning(`${title}已停止，已处理 ${processedOffset}/${targetIds.length} 个账号`)
      } else if (failedCount > 0) {
        toast.warning(`${title}完成，失败 ${failedCount} 个${errors[0] ? `：${errors[0]}` : ''}`)
      } else {
        toast.success(`${title}完成，共刷新 ${targetIds.length} 个账号`)
      }
    } catch (error) {
      options.bulkProgress.fail(
        targetIds.length,
        Math.min(targetIds.length, processedOffset),
        options.normalizeErrorMessage(error),
      )
      options.setError(`${title}失败`, error)
      await options.loadData({ silentErrorToast: true })
    } finally {
      options.bulkProgress.end()
    }
  }

  async function refreshSelectedAccounts() {
    await refreshAccountsWithProgress(options.accountSelection.selectedIds.value, '刷新选中账号信息和额度')
  }

  async function refreshAllAccounts() {
    const title = '刷新所有账号信息和额度'
    const totalHint = options.accountAllTotal.value || options.accountListTotal.value || options.accounts.value.length
    if (!totalHint) {
      toast.warning('没有可刷新的账号')
      return
    }

    const confirmed = await confirmDialog.ask({
      title,
      message: `即将刷新全部 ${totalHint} 个账号的信息和额度，可能触发大量外部 ChatGPT 请求。是否继续？`,
      confirmText: '开始刷新',
      cancelText: '取消',
    })
    if (!confirmed) return

    options.bulkProgress.start(title, totalHint, 'refresh')
    try {
      const result = await accountsApi.refreshAllAccountsWithProgress((progress) => {
        options.bulkProgress.update({
          ...progress,
          total: Number(progress.total || totalHint),
          processed: Number(progress.processed || 0),
          done: false,
        })
      })
      const progress = result.progress
      const errors = normalizeErrorList(progress?.result?.errors)
      options.bulkProgress.finish({
        ...(progress || {}),
        total: Number(progress?.total || totalHint),
        processed: Number(progress?.processed || progress?.total || totalHint),
      })
      await options.loadData({ silentErrorToast: true })
      if (errors.length > 0) {
        toast.warning(`${title}完成，失败 ${errors.length} 个`)
      } else {
        toast.success(`${title}完成`)
      }
    } catch (error) {
      options.bulkProgress.fail(
        totalHint,
        Number(options.bulkProgress.refreshProgress.value?.processed || 0),
        options.normalizeErrorMessage(error),
      )
      options.setError(`${title}失败`, error)
      await options.loadData({ silentErrorToast: true })
    } finally {
      options.bulkProgress.end()
    }
  }

  async function runBulkMutationWithProgress(
    title: string,
    targetIds: string[],
    mutateAccounts: (accountIds: string[]) => Promise<AccountBulkMutationResponse>,
  ): Promise<AccountBulkMutationSummary> {
    options.bulkProgress.start(title, targetIds.length, 'mutation')
    let successCount = 0
    const errors: string[] = []
    const processedIds: string[] = []

    try {
      for (let index = 0; index < targetIds.length; index += options.refreshBatchSize) {
        if (options.bulkProgress.bulkStopRequested.value) break
        const batch = targetIds.slice(index, index + options.refreshBatchSize)
        try {
          const result = await mutateAccounts(batch)
          const batchErrors = normalizeErrorList(result?.errors)
          const batchSuccess = Number(
            result?.success_count ?? result?.updated ?? result?.removed ?? (batch.length - batchErrors.length),
          )
          successCount += Math.max(0, Math.min(batch.length, batchSuccess || 0))
          errors.push(...batchErrors)
        } catch (error) {
          errors.push(`${batch[0]} 等 ${batch.length} 个账号：${options.normalizeErrorMessage(error)}`)
        } finally {
          processedIds.push(...batch)
          const processed = Math.min(targetIds.length, processedIds.length)
          options.bulkProgress.update({
            total: targetIds.length,
            processed,
            done: processed >= targetIds.length,
            total_quota: 0,
          })
        }
      }

      const processed = Math.min(targetIds.length, processedIds.length)
      const stopped = options.bulkProgress.bulkStopRequested.value && processed < targetIds.length
      options.bulkProgress.finish({
        total: targetIds.length,
        processed,
        total_quota: 0,
      })
      return { success_count: successCount, errors, stopped, processed, processed_ids: processedIds }
    } finally {
      options.bulkProgress.end()
    }
  }

  async function runBulkAction(action: AccountBulkAction, ids?: readonly string[]) {
    const targetIds = uniqueIds(ids || options.accountSelection.selectedIds.value)
    if (!targetIds.length) {
      toast.warning('请先选择账号')
      return
    }

    if (action === 'refresh') {
      await refreshAccountsWithProgress(targetIds, '批量刷新账号信息和额度')
      return
    }

    const actionMeta = bulkActionMeta(action)
    const confirmed = await confirmDialog.ask({
      title: actionMeta.title,
      message: `确认对选中的 ${targetIds.length} 个账号执行该操作吗？`,
      confirmText: actionMeta.confirmText,
      cancelText: '取消',
    })
    if (!confirmed) return

    try {
      let result: AccountBulkMutationSummary
      if (action === 'enable') {
        result = await runBulkMutationWithProgress(actionMeta.title, targetIds, accountsApi.bulkEnable)
      } else if (action === 'disable') {
        result = await runBulkMutationWithProgress(actionMeta.title, targetIds, accountsApi.bulkDisable)
      } else if (action === 'delete') {
        result = await runBulkMutationWithProgress(actionMeta.title, targetIds, accountsApi.bulkDelete)
      } else {
        result = await runBulkMutationWithProgress(actionMeta.title, targetIds, accountsApi.bulkEnable)
      }

      const errors = Array.isArray(result.errors) ? result.errors.filter(Boolean) : []
      if (result.stopped) {
        toast.warning(`${actionMeta.title}已停止，已处理 ${result.processed || 0}/${targetIds.length} 个账号`)
      } else if (errors.length > 0) {
        toast.warning(`${actionMeta.successText}，成功 ${result.success_count} 个，失败 ${errors.length} 个`)
      } else {
        toast.success(`${actionMeta.successText}，共 ${result.success_count} 个`)
      }
      if (action === 'delete') {
        const deletedIds = result.stopped ? result.processed_ids : targetIds
        options.accountSelection.removeSelectedIds(deletedIds)
      }
      await options.loadData({ silentErrorToast: true })
      if (action !== 'delete' && result.stopped) {
        options.accountSelection.removeSelectedIds(result.processed_ids)
      } else if (action !== 'delete') {
        options.accountSelection.clearSelection()
      }
    } catch (error) {
      options.setError(`${actionMeta.title}失败`, error)
    }
  }

  async function bindSelectedAccountsToGroup() {
    const targetIds = uniqueIds(options.accountSelection.selectedIds.value)
    if (!targetIds.length) {
      toast.warning('请先选择账号')
      return
    }
    const nextGroupId = options.selectedBindGroupId.value === '__ungrouped__' ? '' : options.selectedBindGroupId.value.trim()
    if (options.selectedBindGroupId.value !== '__ungrouped__' && !nextGroupId) {
      toast.warning('请先选择要绑定的账号组')
      return
    }
    const groupName = nextGroupId
      ? options.accountGroups.value.find((group) => group.id === nextGroupId)?.name || nextGroupId
      : '未分组'
    const confirmed = await confirmDialog.ask({
      title: '批量绑定账号组',
      message: `确认把选中的 ${targetIds.length} 个账号绑定到 ${groupName} 吗？`,
      confirmText: '确认绑定',
      cancelText: '取消',
    })
    if (!confirmed) return

    options.bulkProgress.start('批量绑定账号组', targetIds.length, 'mutation')
    try {
      const result = await accountsApi.bindGroup(targetIds, nextGroupId)
      options.bulkProgress.finish({
        total: targetIds.length,
        processed: targetIds.length,
        total_quota: 0,
      })
      toast.success(`已绑定 ${result.updated || 0} 个账号`)
      options.applyAccountGroupsPayload({ groups: result.groups, proxy_groups: options.proxyGroups.value })
      options.accountSelection.clearSelection()
      await options.loadData({ silentErrorToast: true })
    } catch (error) {
      options.bulkProgress.fail(targetIds.length, 0, options.normalizeErrorMessage(error))
      options.setError('批量绑定账号组失败', error)
    } finally {
      options.bulkProgress.end()
    }
  }

  function requestStopRefreshProgress() {
    if (options.bulkProgress.requestStop()) {
      toast.info('已请求停止，当前批次完成后会停止后续批次')
    }
  }

  return {
    refreshAllAccounts,
    refreshSelectedAccounts,
    requestStopRefreshProgress,
    runBulkAction,
    bindSelectedAccountsToGroup,
  }
}
