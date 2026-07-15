import type { Ref } from 'vue'
import { registerApi, type LegacyRegisterConfig } from '@/api/register'

export type OutlookResetScope = 'all' | 'retryable' | 'invalid' | 'unused'

type ConfirmOptions = {
  title: string
  message: string
  confirmText: string
}

export type RegisterOutlookPoolRuntimeInput = {
  saving: Ref<boolean>
  confirm: (options: ConfirmOptions) => Promise<boolean>
  applyConfig: (config: LegacyRegisterConfig) => void
  notifySuccess: (message: string) => void
  notifyError: (message: string) => void
}

export const outlookPoolActionItems = [
  { key: 'retry_failed', label: '重试临时失败' },
  { key: 'retryable', label: '释放占用/失败' },
  { key: 'invalid', label: '清除异常标记', dividerBefore: true },
  { key: 'unused', label: '清空已用邮箱', danger: true, dividerBefore: true },
  { key: 'all', label: '重置全部邮箱池', danger: true },
]

const resetCopy: Record<OutlookResetScope, ConfirmOptions> = {
  retryable: {
    title: '释放占用/临时失败',
    message: '将释放 in_use 和 failed 邮箱，后续注册任务可以重新使用这些材料。',
    confirmText: '释放',
  },
  invalid: {
    title: '清除异常标记',
    message: '将清除 token_invalid 和 login_required 标记，但不会修复失效的 refresh_token；请确认材料已经重新导入或可重新尝试。',
    confirmText: '清除',
  },
  unused: {
    title: '清空已用邮箱',
    message: '将清空 Outlook 邮箱池中的已用记录，可能导致后续重复尝试同一邮箱。',
    confirmText: '清空',
  },
  all: {
    title: '重置全部邮箱池',
    message: '将重置 Outlook 邮箱池状态，包括可用、占用、失败和已用记录。',
    confirmText: '重置',
  },
}

export function useRegisterOutlookPoolRuntime(input: RegisterOutlookPoolRuntimeInput) {
  async function resetPool(scope: OutlookResetScope) {
    const ok = await input.confirm(resetCopy[scope])
    if (!ok) return
    input.saving.value = true
    try {
      const response = await registerApi.resetOutlookPool(scope)
      input.applyConfig(response.register)
      input.notifySuccess('邮箱池状态已更新')
    } catch (error: any) {
      input.notifyError(error?.message || '邮箱池维护失败')
    } finally {
      input.saving.value = false
    }
  }

  async function retryFailedPool() {
    const ok = await input.confirm({
      title: '重试临时失败邮箱',
      message: '将释放 in_use 和 failed 邮箱，并立即启动注册任务继续重试。',
      confirmText: '重试',
    })
    if (!ok) return
    input.saving.value = true
    try {
      const resetResponse = await registerApi.resetOutlookPool('retryable')
      input.applyConfig(resetResponse.register)
      const startResponse = await registerApi.startLegacy()
      input.applyConfig(startResponse.register)
      input.notifySuccess('已释放临时失败邮箱并启动注册任务')
    } catch (error: any) {
      input.notifyError(error?.message || '重试临时失败邮箱失败')
    } finally {
      input.saving.value = false
    }
  }

  function handleAction(key: string) {
    if (key === 'retry_failed') {
      void retryFailedPool()
      return
    }
    if (key === 'retryable' || key === 'invalid' || key === 'unused' || key === 'all') {
      void resetPool(key)
    }
  }

  return {
    outlookPoolActionItems,
    resetPool,
    retryFailedPool,
    handleAction,
  }
}
