import type { ComputedRef, Ref } from 'vue'

import type { LegacyRegisterConfig, RegisterProvider } from '@/api/register'
import {
  defaultProvider,
  listFromProviderDraft,
  providerType,
  providerWithTypeDraft,
} from '@/views/register/registerProviderView'

type ConfirmOptions = {
  title?: string
  message: string
  confirmText?: string
  cancelText?: string
}

type RegisterProviderRuntimeInput = {
  config: Ref<LegacyRegisterConfig | null>
  providers: ComputedRef<RegisterProvider[]>
  confirm: (options: ConfirmOptions) => Promise<boolean>
  clearGptMailState: (index: number) => void
  clearAllGptMailStates: () => void
}

export function useRegisterProviderRuntime(input: RegisterProviderRuntimeInput) {
  function providerKey(provider: RegisterProvider, index: number) {
    return String(provider.id || provider.provider_id || '').trim() || `${providerType(provider)}-${index}`
  }

  function updateProviderType(index: number, type: string) {
    if (!input.config.value) return
    input.clearGptMailState(index)
    const providers = [...input.providers.value]
    const current = providers[index] || {}
    providers[index] = providerWithTypeDraft(current, type)
    input.config.value.mail.providers = providers
  }

  function updateProviderField(index: number, key: string, value: unknown) {
    const provider = input.providers.value[index]
    if (!provider) return
    provider[key] = value
  }

  function addProvider() {
    if (!input.config.value) return
    input.config.value.mail.providers = [...input.providers.value, defaultProvider()]
  }

  async function deleteProvider(index: number) {
    if (!input.config.value || input.providers.value.length <= 1) return
    const ok = await input.confirm({
      title: '删除邮箱来源',
      message: `确认删除邮箱来源 ${index + 1} 吗？`,
      confirmText: '删除',
    })
    if (!ok) return
    input.clearAllGptMailStates()
    input.config.value.mail.providers = input.providers.value.filter((_, itemIndex) => itemIndex !== index)
  }

  function updateProviderArray(index: number, key: 'domain' | 'subdomain', value: string) {
    const provider = input.providers.value[index]
    if (!provider) return
    provider[key] = listFromProviderDraft(value)
  }

  return {
    providerKey,
    updateProviderType,
    updateProviderField,
    addProvider,
    deleteProvider,
    updateProviderArray,
  }
}
