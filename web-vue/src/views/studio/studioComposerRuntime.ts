import { ref, watch } from 'vue'
import type { StudioComposeMode, StudioMessage } from '@/components/studio/types'
import { getStringPreference, preferenceKeys, setStringPreference } from '@/lib/preferences'

export function normalizeStudioComposeMode(value: string): StudioComposeMode {
  if (value === 'chat' || value === 'search') return value
  return 'image'
}

export function useStudioComposerRuntime() {
  const composeMode = ref<StudioComposeMode>(
    normalizeStudioComposeMode(getStringPreference(preferenceKeys.studioActiveMode, 'image')),
  )
  const composerText = ref('')
  const editingMessageId = ref('')
  const isSending = ref(false)

  watch(composeMode, (mode) => setStringPreference(preferenceKeys.studioActiveMode, mode))

  function cancelMessageEdit(clearComposer = true) {
    editingMessageId.value = ''
    if (clearComposer) composerText.value = ''
  }

  function fillFromMessage(message: StudioMessage) {
    composerText.value = message.content
    composeMode.value = message.mode
  }

  function startEdit(message: StudioMessage) {
    editingMessageId.value = message.id
    composerText.value = message.content
    composeMode.value = message.mode
  }

  function setSending(value: boolean) {
    isSending.value = value
  }

  function activateImageMode() {
    composeMode.value = 'image'
  }

  return {
    activateImageMode,
    cancelMessageEdit,
    composeMode,
    composerText,
    editingMessageId,
    fillFromMessage,
    isSending,
    setSending,
    startEdit,
  }
}
