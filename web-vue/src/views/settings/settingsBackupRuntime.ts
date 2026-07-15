import { ref } from 'vue'

import { settingsApi, type BackupItem, type BackupState, type BackupTestResult } from '@/api/settings'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import type { usePageRuntime } from '@/composables/usePageRuntime'
import { usePageQuery } from '@/composables/usePageQuery'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'

type SettingsBackupRuntimeOptions = {
  runtime: ReturnType<typeof usePageRuntime>
  requestKey: string
  requireSavedSettings: (actionLabel: string) => boolean
}

export function useSettingsBackupRuntime(options: SettingsBackupRuntimeOptions) {
  const backupsLoaded = ref(false)
  const backupBusy = ref('')
  const backupLoading = ref(false)
  const backupState = ref<BackupState | null>(null)
  const backupItems = ref<BackupItem[]>([])
  const backupTestResult = ref<BackupTestResult | null>(null)
  const toast = useToast()
  const confirmDialog = useConfirmDialog()

  const backupsQuery = usePageQuery({
    runtime: options.runtime,
    key: options.requestKey,
    loading: backupLoading,
    errorMessage: '加载备份历史失败',
  })

  async function loadBackups() {
    await backupsQuery.run(
      () => settingsApi.listBackups(),
      {
        apply: (response) => {
          backupItems.value = Array.isArray(response.items) ? response.items : []
          backupState.value = response.state || null
          backupsLoaded.value = true
        },
        onError: (message) => {
          backupItems.value = []
          backupState.value = null
          toast.error(message)
        },
      },
    )
  }

  async function testBackupConnection() {
    if (!options.requireSavedSettings('测试备份连接')) return
    const confirmed = await confirmDialog.ask({
      title: '确认测试备份连接',
      message: '即将使用已保存的备份配置发起 R2/备份存储连接测试，可能访问外部存储服务。是否继续？',
      confirmText: '开始测试',
      cancelText: '取消',
    })
    if (!confirmed) return

    backupBusy.value = 'test'
    backupTestResult.value = null
    try {
      const response = await settingsApi.testBackup()
      backupTestResult.value = response.result
      if (response.result.ok) toast.success('备份连接测试通过')
      else toast.warning(response.result.error || '备份连接测试失败')
    } catch (error) {
      const message = errorMessage(error, '备份连接测试失败')
      backupTestResult.value = { ok: false, error: message }
      toast.error(message)
    } finally {
      backupBusy.value = ''
    }
  }

  async function runBackupNow() {
    if (!options.requireSavedSettings('执行立即备份')) return
    const confirmed = await confirmDialog.ask({
      title: '确认立即备份',
      message: '即将把当前配置和运行数据写入备份存储，可能产生外部上传流量。是否继续？',
      confirmText: '开始备份',
      cancelText: '取消',
    })
    if (!confirmed) return

    backupBusy.value = 'run'
    try {
      const response = await settingsApi.runBackup()
      toast.success(`备份已完成：${response.result.key}`)
      await loadBackups()
    } catch (error) {
      toast.error(errorMessage(error, '执行备份失败'))
    } finally {
      backupBusy.value = ''
    }
  }

  async function deleteBackupItem(item: BackupItem) {
    const confirmed = await confirmDialog.ask({
      title: '删除备份',
      message: `确定删除备份 ${item.name || item.key}？`,
      confirmText: '删除',
      cancelText: '取消',
    })
    if (!confirmed) return

    backupBusy.value = item.key
    try {
      await settingsApi.deleteBackup(item.key)
      toast.success('备份已删除')
      await loadBackups()
    } catch (error) {
      toast.error(errorMessage(error, '删除备份失败'))
    } finally {
      backupBusy.value = ''
    }
  }

  function invalidate() {
    backupsQuery.invalidate()
    backupsLoaded.value = false
  }

  return {
    backupsLoaded,
    backupBusy,
    backupLoading,
    backupState,
    backupItems,
    backupTestResult,
    loadBackups,
    testBackupConnection,
    runBackupNow,
    deleteBackupItem,
    invalidate,
  }
}
