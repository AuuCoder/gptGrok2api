import { computed, reactive, ref, watch } from 'vue'
import {
  DEFAULT_IMAGE_MODEL,
  DEFAULT_IMAGE_QUALITY,
  DEFAULT_IMAGE_SIZE,
  isImageSizeSupportedByModel,
} from '@/api/imageTasks'
import { useModelCatalog } from '@/composables/useModelCatalog'
import {
  getStringPreference,
  preferenceKeys,
  setStringPreference,
} from '@/lib/preferences'
import type { StudioImageForm } from '@/components/studio/types'
import type { useSettingsStore } from '@/stores/settings'

export type StudioModelFormRuntimeInput = {
  settingsStore: ReturnType<typeof useSettingsStore>
}

export function useStudioModelFormRuntime(input: StudioModelFormRuntimeInput) {
  const { chatModels, imageModels, loadModelCatalog } = useModelCatalog(() => input.settingsStore.settings)
  const chatModel = ref(getStringPreference(preferenceKeys.studioChatModel, 'auto') || 'auto')
  const chatReasoningEffort = ref(getStringPreference(preferenceKeys.studioChatReasoningEffort, ''))
  const imageForm = reactive<StudioImageForm>({
    model: getStringPreference(preferenceKeys.studioImageModel, DEFAULT_IMAGE_MODEL) || DEFAULT_IMAGE_MODEL,
    size: DEFAULT_IMAGE_SIZE,
    quality: DEFAULT_IMAGE_QUALITY,
    n: 1,
  })

  const chatModelOptions = computed(() => uniqueStrings(['auto', ...chatModels.value]))
  const imageModelOptions = computed(() => uniqueStrings([imageForm.model, DEFAULT_IMAGE_MODEL, ...imageModels.value]))

  watch(chatModel, (model) => setStringPreference(preferenceKeys.studioChatModel, model || 'auto'))
  watch(chatReasoningEffort, (effort) => setStringPreference(preferenceKeys.studioChatReasoningEffort, effort || ''))
  watch(() => imageForm.model, (model) => {
    setStringPreference(preferenceKeys.studioImageModel, model || DEFAULT_IMAGE_MODEL)
    if (!isImageSizeSupportedByModel(imageForm.size, model)) imageForm.size = DEFAULT_IMAGE_SIZE
  })

  return {
    chatModel,
    chatModelOptions,
    chatReasoningEffort,
    imageForm,
    imageModelOptions,
    loadModelCatalog,
  }
}

function uniqueStrings(values: string[]) {
  return values.map((value) => String(value || '').trim()).filter((value, index, arr) => value && arr.indexOf(value) === index)
}
