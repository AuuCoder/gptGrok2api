import { ref } from 'vue'

import { settingsApi, type ImageStorageTestResult } from '@/api/settings'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'

type SettingsImageStorageRuntimeOptions = {
  requireSavedSettings: (actionLabel: string) => boolean
}

export function useSettingsImageStorageRuntime(options: SettingsImageStorageRuntimeOptions) {
  const imageStorageBusy = ref('')
  const imageStorageTestResult = ref<ImageStorageTestResult | null>(null)
  const toast = useToast()
  const confirmDialog = useConfirmDialog()

  async function testImageStorageConnection() {
    if (!options.requireSavedSettings('测试 WebDAV')) return
    const confirmed = await confirmDialog.ask({
      title: '确认测试 WebDAV',
      message: '即将使用已保存的图片存储配置发起 WebDAV 连接测试，可能访问外部存储服务。是否继续？',
      confirmText: '开始测试',
      cancelText: '取消',
    })
    if (!confirmed) return

    imageStorageBusy.value = 'test'
    imageStorageTestResult.value = null
    try {
      const response = await settingsApi.testImageStorage()
      imageStorageTestResult.value = response.result
      if (response.result.ok) toast.success('WebDAV 测试通过')
      else toast.warning(response.result.error || 'WebDAV 测试失败')
    } catch (error) {
      const message = errorMessage(error, 'WebDAV 测试失败')
      imageStorageTestResult.value = { ok: false, error: message }
      toast.error(message)
    } finally {
      imageStorageBusy.value = ''
    }
  }

  async function syncImageStorageFiles() {
    if (!options.requireSavedSettings('同步本地图片')) return
    const confirmed = await confirmDialog.ask({
      title: '确认同步图片存储',
      message: '即将扫描本地图片并同步到已配置的 WebDAV 存储，可能产生外部上传流量。是否继续？',
      confirmText: '开始同步',
      cancelText: '取消',
    })
    if (!confirmed) return

    imageStorageBusy.value = 'sync'
    try {
      const response = await settingsApi.syncImageStorage()
      toast.success(`同步完成：上传 ${response.result.uploaded}，跳过 ${response.result.skipped}，失败 ${response.result.failed}`)
    } catch (error) {
      toast.error(errorMessage(error, '同步图片失败'))
    } finally {
      imageStorageBusy.value = ''
    }
  }

  return {
    imageStorageBusy,
    imageStorageTestResult,
    testImageStorageConnection,
    syncImageStorageFiles,
  }
}
