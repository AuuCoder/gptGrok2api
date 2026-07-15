import { ref } from 'vue'
import type { LegacyRegisterConfig } from '@/api/register'
import type { PageRuntime } from '@/composables/usePageRuntime'

export type RegisterLiveRuntimeInput = {
  runtime: PageRuntime
  getAuthToken: () => string
  loadConfig: (silent?: boolean) => Promise<void>
  applyConfig: (config: LegacyRegisterConfig) => void
  isTaskEnabled: () => boolean
  hasActiveCheckoutRetries?: () => boolean
}

const POLL_INTERVAL_KEY = 'register:poll'

function eventsBaseUrl() {
  return String(import.meta.env.VITE_API_URL || '').replace(/\/$/, '')
}

export function useRegisterLiveRuntime(input: RegisterLiveRuntimeInput) {
  const eventSource = ref<EventSource | null>(null)

  function shouldKeepUpdating() {
    return input.isTaskEnabled() || Boolean(input.hasActiveCheckoutRetries?.())
  }

  function stopLiveUpdates() {
    if (eventSource.value) {
      eventSource.value.close()
      eventSource.value = null
    }
  }

  function stopPolling() {
    input.runtime.clearInterval(POLL_INTERVAL_KEY)
  }

  function startPolling() {
    stopPolling()
    if (!input.runtime.canRun.value) return
    input.runtime.setInterval(POLL_INTERVAL_KEY, 2000, async () => {
      if (!input.runtime.canRun.value) {
        stopPolling()
        return
      }
      await input.loadConfig(true)
      if (!shouldKeepUpdating()) {
        stopPolling()
      }
    })
  }

  function startLiveUpdates() {
    stopLiveUpdates()
    if (!input.runtime.canRun.value) return
    const token = input.getAuthToken()
    if (!token) {
      startPolling()
      return
    }
    try {
      const source = new EventSource(`${eventsBaseUrl()}/api/register/events?token=${encodeURIComponent(token)}`)
      source.onmessage = (event) => {
        if (!input.runtime.canRun.value) return
        try {
          input.applyConfig(JSON.parse(event.data) as LegacyRegisterConfig)
        } catch {
          // Ignore malformed event payloads and keep the stream alive.
        }
      }
      source.onerror = () => {
        stopLiveUpdates()
        if (!input.runtime.canRun.value) return
        startPolling()
      }
      eventSource.value = source
    } catch {
      startPolling()
    }
  }

  function stop() {
    stopLiveUpdates()
    stopPolling()
  }

  return {
    eventSource,
    startLiveUpdates,
    stopLiveUpdates,
    startPolling,
    stopPolling,
    stop,
  }
}
