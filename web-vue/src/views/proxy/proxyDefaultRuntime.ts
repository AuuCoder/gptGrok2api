import { computed, ref, type Ref } from 'vue'

import { prepareSettingsForEdit, settingsApi } from '@/api/settings'
import {
  parseProxyReference,
  proxyApi,
  serializeProxyReference,
  type ProxyGroup,
  type ProxyTestResult,
} from '@/api/proxy'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { usePageQuery } from '@/composables/usePageQuery'
import type { usePageRuntime } from '@/composables/usePageRuntime'
import { useSettingsStore } from '@/stores/settings'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'
import type { Settings } from '@/types/api'
import {
  normalizeDefaultProxyForCompare,
  proxyGroupOptions as buildProxyGroupOptions,
  toDefaultProxyMode,
  toFallbackProxyMode,
  type DefaultProxyMode,
  type FallbackProxyMode,
} from '@/views/proxy/proxyView'
import { proxyActionError } from '@/views/proxy/proxyGroupRuntime'

type ProxyDefaultRuntimeOptions = {
  runtime: ReturnType<typeof usePageRuntime>
  requestKey: string
  groups: Ref<ProxyGroup[]>
  testingKey: Ref<string>
  updateGroups: (groups: ProxyGroup[]) => void
}

export const DEFAULT_TEST_KEY = '__default__'

function firstSelectValue(value: string | string[]) {
  return Array.isArray(value) ? value[0] : value
}

function defaultProxyFromSettings(settings: Settings) {
  return String(settings.basic?.proxy || settings.proxy || '').trim()
}

function fallbackProxyFromSettings(settings: Settings) {
  return String(settings.fallback_proxy || '').trim()
}

export function useProxyDefaultRuntime(options: ProxyDefaultRuntimeOptions) {
  const settingsStore = useSettingsStore()
  const toast = useToast()
  const confirmDialog = useConfirmDialog()
  const loading = ref(false)
  const savingDefaultProxy = ref(false)
  const defaultProxyMode = ref<DefaultProxyMode>('direct')
  const selectedDefaultProxyGroupId = ref('')
  const defaultCustomProxyInput = ref('')
  const fallbackProxyMode = ref<FallbackProxyMode>('off')
  const selectedFallbackProxyGroupId = ref('')
  const fallbackCustomProxyInput = ref('')
  const currentSettings = ref<Settings | null>(null)
  const defaultTestResult = ref<ProxyTestResult | null>(null)

  const proxyDataQuery = usePageQuery({
    runtime: options.runtime,
    key: options.requestKey,
    loading,
    errorMessage: '加载代理配置失败',
  })

  const defaultProxyGroupOptions = computed(() => (
    buildProxyGroupOptions(options.groups.value, selectedDefaultProxyGroupId.value)
  ))

  const canTestDefaultProxy = computed(() => {
    if (defaultProxyMode.value === 'group') return Boolean(selectedDefaultProxyGroupId.value)
    if (defaultProxyMode.value === 'custom') return Boolean(defaultCustomProxyInput.value.trim())
    return false
  })

  const isDefaultProxyDirty = computed(() => {
    const settings = currentSettings.value
    if (!settings) return false
    return (
      normalizeDefaultProxyForCompare(defaultProxyValue()) !== normalizeDefaultProxyForCompare(defaultProxyFromSettings(settings))
      || normalizeDefaultProxyForCompare(fallbackProxyValue()) !== normalizeDefaultProxyForCompare(fallbackProxyFromSettings(settings))
    )
  })

  function defaultProxyValue() {
    if (defaultProxyMode.value === 'direct') return serializeProxyReference('direct')
    if (defaultProxyMode.value === 'group') return serializeProxyReference('group', selectedDefaultProxyGroupId.value)
    return serializeProxyReference('custom', defaultCustomProxyInput.value)
  }

  function fallbackProxyValue() {
    if (fallbackProxyMode.value === 'off') return ''
    if (fallbackProxyMode.value === 'direct') return serializeProxyReference('direct')
    if (fallbackProxyMode.value === 'group') return serializeProxyReference('group', selectedFallbackProxyGroupId.value)
    return serializeProxyReference('custom', fallbackCustomProxyInput.value)
  }

  function syncDefaultProxyControlsFromValue(value: unknown) {
    const reference = parseProxyReference(value)
    selectedDefaultProxyGroupId.value = ''
    defaultCustomProxyInput.value = ''
    defaultTestResult.value = null
    if (reference.mode === 'group') {
      defaultProxyMode.value = 'group'
      selectedDefaultProxyGroupId.value = reference.value
      return
    }
    if (reference.mode === 'custom' || reference.mode === 'profile') {
      defaultProxyMode.value = 'custom'
      defaultCustomProxyInput.value = reference.mode === 'profile' ? String(value || '').trim() : reference.value
      return
    }
    defaultProxyMode.value = 'direct'
  }

  function syncFallbackProxyControlsFromValue(value: unknown) {
    const reference = parseProxyReference(value)
    selectedFallbackProxyGroupId.value = ''
    fallbackCustomProxyInput.value = ''
    if (reference.mode === 'group') {
      fallbackProxyMode.value = 'group'
      selectedFallbackProxyGroupId.value = reference.value
      return
    }
    if (reference.mode === 'direct') {
      fallbackProxyMode.value = 'direct'
      return
    }
    if (reference.mode === 'custom' || reference.mode === 'profile') {
      fallbackProxyMode.value = 'custom'
      fallbackCustomProxyInput.value = reference.mode === 'profile' ? String(value || '').trim() : reference.value
      return
    }
    fallbackProxyMode.value = 'off'
  }

  function setDefaultProxyMode(mode: string | string[]) {
    defaultProxyMode.value = toDefaultProxyMode(mode)
    defaultTestResult.value = null
  }

  function setFallbackProxyMode(mode: string | string[]) {
    fallbackProxyMode.value = toFallbackProxyMode(mode)
  }

  function selectDefaultProxyGroup(groupId: string | string[]) {
    selectedDefaultProxyGroupId.value = String(firstSelectValue(groupId) || '').trim()
    defaultProxyMode.value = 'group'
    defaultTestResult.value = null
  }

  function selectFallbackProxyGroup(groupId: string | string[]) {
    selectedFallbackProxyGroupId.value = String(firstSelectValue(groupId) || '').trim()
    fallbackProxyMode.value = 'group'
  }

  function setDefaultCustomProxyInput(value: string) {
    defaultCustomProxyInput.value = String(value || '').trim()
    defaultProxyMode.value = 'custom'
    defaultTestResult.value = null
  }

  function setFallbackCustomProxyInput(value: string) {
    fallbackCustomProxyInput.value = String(value || '').trim()
    fallbackProxyMode.value = 'custom'
  }

  async function loadData() {
    await proxyDataQuery.run(
      () => Promise.all([
        settingsApi.get(),
        proxyApi.listGroups(),
      ]),
      {
        apply: ([settings, groupResponse]) => {
          currentSettings.value = prepareSettingsForEdit(settings)
          settingsStore.$patch({ settings })
          options.updateGroups(groupResponse.groups || [])
          syncDefaultProxyControlsFromValue(defaultProxyFromSettings(settings))
          syncFallbackProxyControlsFromValue(fallbackProxyFromSettings(settings))
        },
        onError: (message) => {
          toast.error(message)
        },
      },
    )
  }

  async function saveDefaultProxy() {
    if (!currentSettings.value) {
      toast.warning('配置尚未加载完成')
      return
    }
    if (defaultProxyMode.value === 'group' && !selectedDefaultProxyGroupId.value) {
      toast.warning('请选择默认出口代理组')
      return
    }
    if (defaultProxyMode.value === 'custom' && !defaultCustomProxyInput.value.trim()) {
      toast.warning('请填写自定义代理 URL')
      return
    }
    if (fallbackProxyMode.value === 'group' && !selectedFallbackProxyGroupId.value) {
      toast.warning('请选择备用出口代理组')
      return
    }
    if (fallbackProxyMode.value === 'custom' && !fallbackCustomProxyInput.value.trim()) {
      toast.warning('请填写备用代理 URL')
      return
    }
    const confirmed = await confirmDialog.ask({
      title: '确认保存出口配置',
      message: '即将保存默认出口和备用出口配置。备用出口只在图片请求早期连接失败时重试一次，是否继续？',
      confirmText: '保存',
      cancelText: '取消',
    })
    if (!confirmed) return

    savingDefaultProxy.value = true
    try {
      const next = prepareSettingsForEdit(currentSettings.value)
      next.proxy = defaultProxyValue()
      next.fallback_proxy = fallbackProxyValue()
      const response = await settingsStore.updateSettingsPatch({
        proxy: next.proxy,
        fallback_proxy: next.fallback_proxy,
      })
      currentSettings.value = prepareSettingsForEdit(response.config || next)
      syncDefaultProxyControlsFromValue(defaultProxyFromSettings(currentSettings.value))
      syncFallbackProxyControlsFromValue(fallbackProxyFromSettings(currentSettings.value))
      toast.success('出口配置已保存')
    } catch (error) {
      toast.error(proxyActionError('保存出口配置失败', error))
    } finally {
      savingDefaultProxy.value = false
    }
  }

  function setDefaultProxyDirect() {
    defaultProxyMode.value = 'direct'
    selectedDefaultProxyGroupId.value = ''
    defaultCustomProxyInput.value = ''
    defaultTestResult.value = null
  }

  async function testDefaultProxy() {
    if (defaultProxyMode.value === 'direct') {
      toast.info('直连模式无需测试出口')
      return
    }
    if (defaultProxyMode.value === 'group' && !selectedDefaultProxyGroupId.value) {
      toast.warning('请选择默认出口代理组')
      return
    }
    if (defaultProxyMode.value === 'custom' && !defaultCustomProxyInput.value.trim()) {
      toast.warning('请先填写自定义代理 URL')
      return
    }
    const confirmed = await confirmDialog.ask({
      title: '确认测试默认出口',
      message: '即将使用当前默认出口发起外部网络测试请求。请确认当前允许测试该出口连接。',
      confirmText: '开始测试',
      cancelText: '取消',
    })
    if (!confirmed) return

    options.testingKey.value = DEFAULT_TEST_KEY
    try {
      if (defaultProxyMode.value === 'group') {
        const response = await proxyApi.testGroup({ id: selectedDefaultProxyGroupId.value })
        if (response.groups) options.updateGroups(response.groups)
        const results = response.results || []
        const failed = results.filter((item) => !item.result.ok)
        const firstResult = results[0]?.result
        const maxLatency = results.reduce((max, item) => Math.max(max, Number(item.result.latency_ms || 0)), 0)
        defaultTestResult.value = {
          ok: results.length > 0 && failed.length === 0,
          status: firstResult?.status || 0,
          latency_ms: maxLatency,
          error: failed.length ? `代理组检测完成，失败 ${failed.length} 个节点` : null,
        }
        if (defaultTestResult.value.ok) toast.success(`默认出口代理组可用，共 ${results.length} 个节点`)
        else toast.warning(defaultTestResult.value.error || '默认出口代理组测试失败')
        return
      }
      const response = await proxyApi.test(defaultCustomProxyInput.value.trim())
      defaultTestResult.value = response.result
      if (response.result.ok) toast.success(`默认出口可用，耗时 ${response.result.latency_ms}ms`)
      else toast.warning(response.result.error || '默认出口测试失败')
    } catch (error) {
      const message = errorMessage(error, '默认出口测试失败')
      defaultTestResult.value = {
        ok: false,
        status: 0,
        latency_ms: 0,
        error: message,
      }
      toast.error(message)
    } finally {
      options.testingKey.value = ''
    }
  }

  function invalidate() {
    proxyDataQuery.invalidate()
  }

  return {
    loading,
    savingDefaultProxy,
    defaultProxyMode,
    selectedDefaultProxyGroupId,
    defaultCustomProxyInput,
    fallbackProxyMode,
    selectedFallbackProxyGroupId,
    fallbackCustomProxyInput,
    defaultTestResult,
    defaultProxyGroupOptions,
    canTestDefaultProxy,
    isDefaultProxyDirty,
    setDefaultProxyMode,
    setFallbackProxyMode,
    selectDefaultProxyGroup,
    selectFallbackProxyGroup,
    setDefaultCustomProxyInput,
    setFallbackCustomProxyInput,
    loadData,
    saveDefaultProxy,
    setDefaultProxyDirect,
    testDefaultProxy,
    invalidate,
  }
}
