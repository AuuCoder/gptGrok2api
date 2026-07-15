import { ref, type Ref } from 'vue'

import { normalizeProxyRuntime } from '@/api/settings'
import {
  parseProxyReference,
  proxyApi,
  type ClearanceTestResult,
  type ProxyRuntimeStatus,
  type ProxyTestResult,
} from '@/api/proxy'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { usePageQuery } from '@/composables/usePageQuery'
import type { usePageRuntime } from '@/composables/usePageRuntime'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'
import type { Settings } from '@/types/api'

type SettingsProxyRuntimeOptions = {
  runtime: ReturnType<typeof usePageRuntime>
  requestKey: string
  localSettings: Ref<Settings | null>
  requireSavedSettings: (actionLabel: string) => boolean
}

export function useSettingsProxyRuntime(options: SettingsProxyRuntimeOptions) {
  const proxyBusy = ref('')
  const proxyTestResult = ref<ProxyTestResult | null>(null)
  const proxyRuntimeLoading = ref(false)
  const proxyRuntimeTesting = ref(false)
  const proxyRuntimeStatus = ref<ProxyRuntimeStatus | null>(null)
  const clearanceTestTarget = ref('https://chatgpt.com')
  const clearanceTestResult = ref<ClearanceTestResult | null>(null)
  const toast = useToast()
  const confirmDialog = useConfirmDialog()

  const proxyRuntimeQuery = usePageQuery({
    runtime: options.runtime,
    key: options.requestKey,
    loading: proxyRuntimeLoading,
    errorMessage: '加载稳定代理状态失败',
  })

  async function testDefaultProxy() {
    const candidate = String(options.localSettings.value?.proxy || '').trim()
    const reference = parseProxyReference(candidate)
    if (reference.mode === 'global' || reference.mode === 'direct') {
      toast.info('直连模式无需测试出口')
      return
    }
    if (reference.mode === 'group' && !reference.value) {
      toast.warning('请填写代理组 ID')
      return
    }
    if ((reference.mode === 'custom' || reference.mode === 'profile') && !reference.value) {
      toast.warning('请先填写默认出口')
      return
    }
    const confirmed = await confirmDialog.ask({
      title: '测试默认出口',
      message: '即将使用当前填写的默认出口发起连接测试，不会保存设置。是否继续？',
      confirmText: '开始测试',
      cancelText: '取消',
    })
    if (!confirmed) return

    proxyBusy.value = 'test'
    proxyTestResult.value = null
    try {
      if (reference.mode === 'group') {
        const response = await proxyApi.testGroup({ id: reference.value })
        const results = response.results || []
        const failed = results.filter((item) => !item.result.ok)
        const firstResult = results[0]?.result
        proxyTestResult.value = {
          ok: results.length > 0 && failed.length === 0,
          status: firstResult?.status || 0,
          latency_ms: results.reduce((max, item) => Math.max(max, Number(item.result.latency_ms || 0)), 0),
          error: failed.length ? `代理组检测完成，失败 ${failed.length} 个节点` : null,
        }
        if (proxyTestResult.value.ok) {
          toast.success(`默认出口代理组可用：${results.length} 个节点`)
        } else {
          toast.warning(proxyTestResult.value.error || '代理组测试失败')
        }
        return
      }
      if (reference.mode === 'profile') {
        const response = await proxyApi.testProfile({ id: reference.value })
        proxyTestResult.value = response.result
        if (response.result.ok) {
          toast.success(`出口可用：${response.result.latency_ms} ms`)
        } else {
          toast.warning(response.result.error || '出口测试失败')
        }
        return
      }
      const response = await proxyApi.test(candidate)
      proxyTestResult.value = response.result
      if (response.result.ok) {
        toast.success(`出口可用：${response.result.latency_ms} ms`)
      } else {
        toast.warning(response.result.error || '出口测试失败')
      }
    } catch (error) {
      proxyTestResult.value = {
        ok: false,
        status: 0,
        latency_ms: 0,
        error: errorMessage(error, '出口测试失败'),
      }
      toast.error(errorMessage(error, '出口测试失败'))
    } finally {
      proxyBusy.value = ''
    }
  }

  async function loadProxyRuntimeStatus(silent = false) {
    await proxyRuntimeQuery.run(
      () => proxyApi.getRuntime(),
      {
        apply: (response) => {
          proxyRuntimeStatus.value = response.status
          if (options.localSettings.value && !options.localSettings.value.proxy_runtime) {
            options.localSettings.value.proxy_runtime = normalizeProxyRuntime(response.runtime)
          }
        },
        onError: (message) => {
          proxyRuntimeStatus.value = null
          if (!silent) toast.error(message)
        },
      },
    )
  }

  async function testProxyClearance() {
    if (!options.requireSavedSettings('测试 Cloudflare 清障')) return
    proxyRuntimeTesting.value = true
    clearanceTestResult.value = null
    try {
      const response = await proxyApi.testClearance(clearanceTestTarget.value)
      clearanceTestResult.value = response.result
      if (response.result.runtime) proxyRuntimeStatus.value = response.result.runtime
      if (response.result.ok) {
        toast.success(`Cloudflare 清障可用：${response.result.latency_ms} ms`)
      } else {
        toast.warning(response.result.error || 'Cloudflare 清障测试失败')
      }
    } catch (error) {
      const message = errorMessage(error, 'Cloudflare 清障测试失败')
      clearanceTestResult.value = {
        ok: false,
        status: 'error',
        latency_ms: 0,
        has_cookies: false,
        user_agent: '',
        error: message,
      }
      toast.error(message)
    } finally {
      proxyRuntimeTesting.value = false
    }
  }

  function invalidate() {
    proxyRuntimeQuery.invalidate()
  }

  return {
    proxyBusy,
    proxyTestResult,
    proxyRuntimeLoading,
    proxyRuntimeTesting,
    proxyRuntimeStatus,
    clearanceTestTarget,
    clearanceTestResult,
    testDefaultProxy,
    loadProxyRuntimeStatus,
    testProxyClearance,
    invalidate,
  }
}
