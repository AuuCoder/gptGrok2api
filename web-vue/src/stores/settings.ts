import { defineStore } from 'pinia'
import { ref } from 'vue'
import { prepareSettingsForEdit, settingsApi, type RawSettings } from '@/api/settings'
import type { Settings, SettingsUpdateResponse } from '@/types/api'

export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<Settings | null>(null)
  const isLoading = ref(false)
  let loadSeq = 0

  async function loadSettings() {
    const seq = ++loadSeq
    isLoading.value = true
    try {
      const nextSettings = await settingsApi.get()
      if (seq === loadSeq) settings.value = nextSettings
    } finally {
      if (seq === loadSeq) isLoading.value = false
    }
  }

  async function updateSettings(newSettings: Settings): Promise<SettingsUpdateResponse> {
    loadSeq += 1
    const response = await settingsApi.update(newSettings)
    settings.value = prepareSettingsForEdit(response.config || newSettings)
    isLoading.value = false
    return response
  }

  async function updateSettingsPatch(patch: RawSettings): Promise<SettingsUpdateResponse> {
    loadSeq += 1
    const response = await settingsApi.updatePartial(patch)
    settings.value = prepareSettingsForEdit(response.config || settings.value)
    isLoading.value = false
    return response
  }

  return {
    settings,
    isLoading,
    loadSettings,
    updateSettings,
    updateSettingsPatch,
  }
})
