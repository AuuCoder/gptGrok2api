import { reactive, ref, type Ref } from 'vue'

import { galleryApi, type GalleryFile, type ImageStorageStats } from '@/api/gallery'
import { saveBlob } from '@/lib/downloads'
import {
  formatCleanupExpiredMessage,
  formatCleanupTargetMessage,
  formatCompressStorageMessage,
  formatMb,
} from '@/views/gallery/galleryView'
import type { PageRuntime } from '@/composables/usePageRuntime'
import { usePageQuery } from '@/composables/usePageQuery'

type ConfirmDialog = {
  ask: (options: {
    title: string
    message: string
    confirmText?: string
    cancelText?: string
  }) => Promise<boolean>
}

type Toast = {
  success: (message: string, title?: string) => void
  error: (message: string, title?: string) => void
}

type GalleryOperationsRuntimeOptions = {
  runtime: PageRuntime
  confirmDialog: ConfirmDialog
  toast: Toast
  files: Ref<GalleryFile[]>
  currentPage: Ref<number>
  storageStats: Ref<ImageStorageStats | null>
  selectedPaths: Ref<Set<string>>
  loadGallery: () => Promise<void>
  closePreviewIfPath: (path: string) => void
  closeTagEditorIfPath: (path: string) => void
  clearSelection: () => void
}

export function useGalleryOperationsRuntime(options: GalleryOperationsRuntimeOptions) {
  const isStorageModalOpen = ref(false)
  const isStorageBusy = ref(false)
  const storageActionMessage = ref('')
  const storageActionError = ref('')
  const targetFreeMb = ref('500')
  const batchBusy = ref(false)
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

  const storageStatsQuery = usePageQuery({
    runtime: options.runtime,
    key: 'gallery:storage',
    error: storageActionError,
    errorMessage: '刷新存储统计失败',
  })

  function resetProgress(config: {
    title: string
    subtitle: string
    total: number
    message: string
  }) {
    operationProgress.open = true
    operationProgress.title = config.title
    operationProgress.subtitle = config.subtitle
    operationProgress.total = config.total
    operationProgress.current = 0
    operationProgress.statusLabel = '已提交'
    operationProgress.message = config.message
    operationProgress.error = ''
    operationProgress.busy = true
  }

  async function refreshStorageStats(optionsOverride: { lock?: boolean; silent?: boolean } = {}) {
    if (!options.runtime.isActive.value) return
    const shouldLock = optionsOverride.lock !== false
    if (shouldLock) isStorageBusy.value = true
    if (!optionsOverride.silent) storageActionError.value = ''
    await storageStatsQuery.run(
      () => galleryApi.getStorage(),
      {
        apply: (nextStats) => {
          options.storageStats.value = nextStats
        },
        onError: (message) => {
          options.toast.error(message, '刷新失败')
        },
        onSettled: (latest) => {
          if (shouldLock && latest) isStorageBusy.value = false
        },
        silentError: optionsOverride.silent,
      },
    )
  }

  function openStorageModal() {
    isStorageModalOpen.value = true
    storageActionMessage.value = ''
    storageActionError.value = ''
    void refreshStorageStats()
  }

  function closeStorageModal() {
    if (isStorageBusy.value) return
    isStorageModalOpen.value = false
  }

  async function handleCompressStorage() {
    const confirmed = await options.confirmDialog.ask({
      title: '压缩图片',
      message: '将尝试压缩本地图片以释放空间。该操作可能需要一点时间，确定继续吗？',
      confirmText: '开始压缩',
      cancelText: '取消',
    })
    if (!confirmed) return

    isStorageBusy.value = true
    storageActionMessage.value = '正在压缩图片...'
    storageActionError.value = ''
    try {
      const result = await galleryApi.compressStorage()
      storageActionMessage.value = formatCompressStorageMessage(result)
      options.toast.success(storageActionMessage.value, '压缩完成')
      await Promise.all([refreshStorageStats({ lock: false }), options.loadGallery()])
    } catch (error: any) {
      storageActionError.value = error?.message || '压缩图片失败'
      options.toast.error(storageActionError.value, '压缩失败')
    } finally {
      isStorageBusy.value = false
    }
  }

  async function handleCleanupExpired() {
    const confirmed = await options.confirmDialog.ask({
      title: '清理过期图片',
      message: '将删除图库中已过期的图片记录和文件。此操作不可恢复，确定继续吗？',
      confirmText: '清理过期',
      cancelText: '取消',
    })
    if (!confirmed) return

    isStorageBusy.value = true
    storageActionMessage.value = '正在清理过期图片...'
    storageActionError.value = ''
    try {
      const result = await galleryApi.cleanupExpired()
      storageActionMessage.value = formatCleanupExpiredMessage(result)
      options.toast.success(storageActionMessage.value, '清理完成')
      await Promise.all([refreshStorageStats({ lock: false }), options.loadGallery()])
    } catch (error: any) {
      storageActionError.value = error?.message || '清理过期图片失败'
      options.toast.error(storageActionError.value, '清理失败')
    } finally {
      isStorageBusy.value = false
    }
  }

  async function handleCleanupToTarget(dryRun: boolean) {
    const target = Number(targetFreeMb.value)
    if (!Number.isFinite(target) || target < 1) {
      storageActionError.value = '请输入有效的目标剩余空间。'
      options.toast.error(storageActionError.value, '参数错误')
      return
    }

    const normalizedTarget = Math.floor(target)
    if (!dryRun) {
      const confirmed = await options.confirmDialog.ask({
        title: '清理到目标空间',
        message: `将从旧图片开始清理，直到磁盘剩余空间尽量达到 ${formatMb(normalizedTarget)}。此操作不可恢复，确定继续吗？`,
        confirmText: '开始清理',
        cancelText: '取消',
      })
      if (!confirmed) return
    }

    isStorageBusy.value = true
    storageActionMessage.value = dryRun ? '正在预估可清理图片...' : '正在清理到目标剩余空间...'
    storageActionError.value = ''
    try {
      const result = await galleryApi.cleanupToTarget(normalizedTarget, dryRun)
      storageActionMessage.value = formatCleanupTargetMessage(result, { dryRun, normalizedTarget })
      if (dryRun) {
        options.toast.success(storageActionMessage.value, '预估完成')
        await refreshStorageStats({ lock: false })
      } else {
        options.toast.success(storageActionMessage.value, '清理完成')
        await Promise.all([refreshStorageStats({ lock: false }), options.loadGallery()])
      }
    } catch (error: any) {
      storageActionError.value = error?.message || '按目标剩余空间清理失败'
      options.toast.error(storageActionError.value, '清理失败')
    } finally {
      isStorageBusy.value = false
    }
  }

  async function handleDelete(file: GalleryFile) {
    const confirmed = await options.confirmDialog.ask({
      title: '确认删除',
      message: `确定要删除 ${file.filename} 吗？此操作不可恢复。`,
      confirmText: '删除',
      cancelText: '取消',
    })
    if (!confirmed) return

    batchBusy.value = true
    resetProgress({
      title: '删除图片',
      subtitle: file.filename,
      total: 1,
      message: '正在提交删除请求...',
    })
    try {
      await galleryApi.deleteFile(file.path)
      operationProgress.current = 1
      operationProgress.statusLabel = '已处理'
      operationProgress.message = '删除完成，正在刷新列表...'
      options.selectedPaths.value.delete(file.path)
      options.selectedPaths.value = new Set(options.selectedPaths.value)
      options.closePreviewIfPath(file.path)
      options.closeTagEditorIfPath(file.path)
      if (options.files.value.length === 1 && options.currentPage.value > 1) {
        options.currentPage.value -= 1
      } else {
        await options.loadGallery()
      }
      options.toast.success(`已删除 ${file.filename}`, '删除成功')
      operationProgress.message = '图片已删除'
    } catch (error: any) {
      operationProgress.error = error?.message || '删除图片失败'
      options.toast.error(operationProgress.error, '删除失败')
    } finally {
      batchBusy.value = false
      operationProgress.busy = false
    }
  }

  async function handleDeleteSelected() {
    const paths = Array.from(options.selectedPaths.value)
    if (!paths.length) return
    const confirmed = await options.confirmDialog.ask({
      title: '批量删除',
      message: `确定要删除已选择的 ${paths.length} 张图片吗？此操作不可恢复。`,
      confirmText: '删除',
      cancelText: '取消',
    })
    if (!confirmed) return

    batchBusy.value = true
    resetProgress({
      title: '批量删除图片',
      subtitle: `已选择 ${paths.length} 张`,
      total: paths.length,
      message: '正在提交批量删除请求...',
    })
    try {
      const result = await galleryApi.deleteFiles(paths)
      operationProgress.current = Number(result.removed || 0)
      operationProgress.statusLabel = '已处理'
      operationProgress.message = '删除完成，正在刷新列表...'
      options.clearSelection()
      await options.loadGallery()
      options.toast.success(`已删除 ${Number(result.removed || 0)} 张图片。`, '删除成功')
      operationProgress.message = `已删除 ${Number(result.removed || 0)} 张图片`
    } catch (error: any) {
      operationProgress.error = error?.message || '批量删除失败'
      options.toast.error(operationProgress.error, '删除失败')
    } finally {
      batchBusy.value = false
      operationProgress.busy = false
    }
  }

  async function handleBatchDownload() {
    const paths = Array.from(options.selectedPaths.value)
    if (!paths.length) return

    batchBusy.value = true
    resetProgress({
      title: '批量下载图片',
      subtitle: `已选择 ${paths.length} 张`,
      total: paths.length,
      message: '正在打包 ZIP...',
    })
    try {
      const blob = await galleryApi.downloadZip(paths)
      operationProgress.current = paths.length
      operationProgress.statusLabel = '已处理'
      operationProgress.message = 'ZIP 已生成，正在启动下载...'
      saveBlob(blob, `images_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.zip`)
      options.toast.success(`已打包 ${paths.length} 张图片。`, '下载已开始')
      operationProgress.message = `已打包 ${paths.length} 张图片`
    } catch (error: any) {
      operationProgress.error = error?.message || '批量下载失败'
      options.toast.error(operationProgress.error, '下载失败')
    } finally {
      batchBusy.value = false
      operationProgress.busy = false
    }
  }

  function deactivate() {
    storageStatsQuery.invalidate()
    isStorageBusy.value = false
  }

  return {
    batchBusy,
    isStorageModalOpen,
    isStorageBusy,
    storageActionMessage,
    storageActionError,
    targetFreeMb,
    operationProgress,
    refreshStorageStats,
    openStorageModal,
    closeStorageModal,
    handleCompressStorage,
    handleCleanupExpired,
    handleCleanupToTarget,
    handleDelete,
    handleDeleteSelected,
    handleBatchDownload,
    deactivate,
  }
}
