import { ref, watch, type Ref } from 'vue'
import { normalizeNumberSetting, type NumberSettingOptions } from './settingsView'

export type NumberSettingField = {
  input: Ref<string>
  update: (value: string) => void
}

export function useNumberSettingField(
  getter: () => number,
  setter: (value: number) => void,
  options: NumberSettingOptions = {},
): NumberSettingField {
  const input = ref('')

  watch(getter, (value) => {
    const next = String(value)
    if (input.value !== next) {
      input.value = next
    }
  }, { immediate: true })

  const update = (value: string) => {
    input.value = value
    const parsed = Number(value)
    if (value.trim() === '' || !Number.isFinite(parsed)) return
    const min = options.min ?? 0
    const fallback = options.fallback ?? getter()
    const next = normalizeNumberSetting(parsed, { ...options, fallback, min })
    setter(next)
  }

  return { input, update }
}
