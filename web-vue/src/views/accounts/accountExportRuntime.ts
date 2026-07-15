import { ref, type Ref } from 'vue'

import { accountsApi, type Account } from '@/api/accounts'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import { saveBlob } from '@/lib/downloads'

type AccountExportScope = 'selected' | 'all' | 'auto'

type AccountExportRuntimeOptions = {
  accounts: Ref<Account[]>
  selectedIds: Ref<string[]>
  accountAllTotal: Ref<number>
  accountListTotal: Ref<number>
  setError: (prefix: string, error: unknown, notify?: boolean) => void
}

function createExportFilename(extension = 'json') {
  const now = new Date()
  const parts = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
    '-',
    String(now.getHours()).padStart(2, '0'),
    String(now.getMinutes()).padStart(2, '0'),
    String(now.getSeconds()).padStart(2, '0'),
  ]
  return `accounts-export-${parts.join('')}.${extension}`
}

function uniqueTokens(tokens: string[]) {
  return Array.from(new Set(tokens.map((token) => token.trim()).filter(Boolean)))
}

export function useAccountExportRuntime(options: AccountExportRuntimeOptions) {
  const exportBusy = ref(false)
  const toast = useToast()
  const confirmDialog = useConfirmDialog()

  async function exportAccounts(scope: AccountExportScope = 'auto') {
    const targetIds = new Set(scope === 'all' ? [] : options.selectedIds.value)
    if (scope === 'all' || (scope === 'auto' && targetIds.size === 0)) {
      const totalHint = options.accountAllTotal.value || options.accountListTotal.value || options.accounts.value.length
      if (!totalHint) {
        toast.warning('暂无可导出的账号')
        return
      }
      const confirmed = await confirmDialog.ask({
        title: '导出全部账号认证',
        message: `即将导出全部 ${totalHint} 个账号。导出文件可能包含 refresh_token、id_token 或 access token，请只在可信环境保存。是否继续？`,
        confirmText: '确认导出',
        cancelText: '取消',
      })
      if (!confirmed) return

      exportBusy.value = true
      try {
        const blob = await accountsApi.exportAccounts([], 'json')
        saveBlob(blob, createExportFilename('json'))
        toast.success('已导出全部账号认证')
      } catch (error) {
        options.setError('导出失败', error)
      } finally {
        exportBusy.value = false
      }
      return
    }
    if (scope === 'selected' && targetIds.size === 0) {
      toast.warning('请先选择要导出的账号')
      return
    }

    const targetAccounts = targetIds.size
      ? options.accounts.value.filter((item) => targetIds.has(item.id))
      : options.accounts.value

    if (!targetAccounts.length) {
      toast.warning('暂无可导出的账号')
      return
    }

    const exportScopeLabel = targetIds.size === 0 ? '全部' : '选中'
    const confirmed = await confirmDialog.ask({
      title: '导出账号认证',
      message: `即将导出${exportScopeLabel} ${targetAccounts.length} 个账号。导出文件可能包含 refresh_token、id_token 或 access token，请只在可信环境保存。是否继续？`,
      confirmText: '确认导出',
      cancelText: '取消',
    })
    if (!confirmed) return

    exportBusy.value = true
    try {
      const blob = await accountsApi.exportAccounts(targetAccounts.map((item) => item.id), 'json')
      saveBlob(blob, createExportFilename('json'))
      toast.success(`已导出 ${targetAccounts.length} 个完整认证账号`)
    } catch (error) {
      const status = (error as { status?: number }).status
      if (status !== 400) {
        options.setError('导出失败', error)
        return
      }

      const tokens = uniqueTokens(targetAccounts.map((item) => item.access_token || ''))
      if (!tokens.length) {
        options.setError('导出失败', error)
        return
      }

      saveBlob(new Blob([`${tokens.join('\n')}\n`], { type: 'text/plain;charset=utf-8' }), createExportFilename('txt'))
      toast.warning(`没有可导出的完整认证 JSON，已改为导出 ${tokens.length} 个 Access Token`)
    } finally {
      exportBusy.value = false
    }
  }

  return {
    exportBusy,
    exportAccounts,
  }
}
