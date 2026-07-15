import { ref, type Ref } from 'vue'
import { registerApi, type GptMailStatus, type RegisterProvider } from '@/api/register'
import type { PageRuntime } from '@/composables/usePageRuntime'
import {
  formatClock,
  gptMailRemainingText as buildGptMailRemainingText,
  gptMailResetText as buildGptMailResetText,
  gptMailSecondsUntilReset,
  gptMailStatusHint as buildGptMailStatusHint,
  gptMailStatusTitle as buildGptMailStatusTitle,
  gptMailStatusTone as buildGptMailStatusTone,
  providerType,
  sanitizedProviderPayload,
  type GptMailStatusState,
} from '@/views/register/registerProviderView'

export type GptMailCheckOptions = {
  silent?: boolean
  force?: boolean
  reschedule?: boolean
}

export type RegisterGptMailRuntimeInput = {
  runtime: PageRuntime
  providers: Ref<RegisterProvider[]>
  notifySuccess: (message: string) => void
  notifyError: (message: string) => void
}

const CLOCK_INTERVAL_KEY = 'register:gptmail-clock'
const REFRESH_TIMER_PREFIX = 'register:gptmail-refresh'
const RESET_FALLBACK_SECONDS = 5 * 60

function gptMailTimerDelay(seconds: number) {
  return Math.min(Math.max(seconds * 1000, 1000), 2_147_483_000)
}

function gptMailResetDelays(status: GptMailStatus) {
  const seconds = gptMailSecondsUntilReset(status, Date.now())
  if (seconds === null) return []
  const resetSeconds = Math.max(0, seconds)
  return [
    { delay: gptMailTimerDelay(resetSeconds), reschedule: false },
    { delay: gptMailTimerDelay(resetSeconds + RESET_FALLBACK_SECONDS), reschedule: true },
  ]
}

export function useRegisterGptMailRuntime(input: RegisterGptMailRuntimeInput) {
  const statusStates = ref<Record<number, GptMailStatusState>>({})
  const clockNow = ref(Date.now())
  const refreshTimerKeys = new Map<number, string[]>()

  function state(index: number): GptMailStatusState {
    return statusStates.value[index] || { loading: false, error: '', data: null }
  }

  function setState(index: number, next: GptMailStatusState) {
    statusStates.value = { ...statusStates.value, [index]: next }
  }

  function clearRefreshTimer(index: number) {
    const timerKeys = refreshTimerKeys.get(index) || []
    timerKeys.forEach((key) => input.runtime.clearTimer(key))
    if (timerKeys.length) {
      refreshTimerKeys.delete(index)
    }
  }

  function clearAllRefreshTimers() {
    refreshTimerKeys.forEach((timerKeys) => timerKeys.forEach((key) => input.runtime.clearTimer(key)))
    refreshTimerKeys.clear()
  }

  function clearState(index: number) {
    clearRefreshTimer(index)
    const next = { ...statusStates.value }
    delete next[index]
    statusStates.value = next
  }

  function clearAllStates() {
    clearAllRefreshTimers()
    statusStates.value = {}
  }

  function pruneStates() {
    const next: Record<number, GptMailStatusState> = {}
    Object.entries(statusStates.value).forEach(([key, value]) => {
      const index = Number(key)
      const provider = input.providers.value[index]
      if (provider && providerType(provider) === 'gptmail') {
        next[index] = value
      } else {
        clearRefreshTimer(index)
      }
    })
    Array.from(refreshTimerKeys.keys()).forEach((index) => {
      const provider = input.providers.value[index]
      if (!provider || providerType(provider) !== 'gptmail') clearRefreshTimer(index)
    })
    statusStates.value = next
  }

  function scheduleRefresh(index: number, status: GptMailStatus) {
    clearRefreshTimer(index)
    if (!input.runtime.canRun.value) return
    if (String(status.key_mode || 'public') !== 'public') return
    const timerKeys = gptMailResetDelays(status).map(({ delay, reschedule }, timerIndex) => {
      const timerKey = `${REFRESH_TIMER_PREFIX}:${index}:${timerIndex}`
      input.runtime.setTimer(timerKey, delay, () => {
        const activeTimers = refreshTimerKeys.get(index) || []
        const nextTimers = activeTimers.filter((item) => item !== timerKey)
        if (nextTimers.length) {
          refreshTimerKeys.set(index, nextTimers)
        } else {
          refreshTimerKeys.delete(index)
        }
        if (!input.runtime.canRun.value) return
        const provider = input.providers.value[index]
        if (!provider || providerType(provider) !== 'gptmail') return
        void refreshPublicKey(index, provider, { reschedule })
      })
      return timerKey
    })
    if (timerKeys.length) refreshTimerKeys.set(index, timerKeys)
  }

  function statusByIndex(index: number) {
    return state(index).data
  }

  function statusBusy(index: number) {
    return state(index).loading
  }

  function statusTone(index: number) {
    return buildGptMailStatusTone(state(index))
  }

  function statusTitle(index: number, provider: RegisterProvider) {
    return buildGptMailStatusTitle(state(index), provider)
  }

  function remainingText(index: number) {
    return buildGptMailRemainingText(statusByIndex(index))
  }

  function resetText(index: number) {
    return buildGptMailResetText(statusByIndex(index), clockNow.value, formatClock)
  }

  function statusHint(index: number, provider: RegisterProvider) {
    return buildGptMailStatusHint(state(index), provider, formatClock)
  }

  async function checkStatus(index: number, provider: RegisterProvider, options: GptMailCheckOptions = {}) {
    const previous = state(index).data
    setState(index, { ...state(index), loading: true, error: '' })
    try {
      const response = await registerApi.getGptMailStatus(sanitizedProviderPayload(provider), options.force ?? true)
      setState(index, { loading: false, error: '', data: response.status })
      if (options.reschedule !== false) scheduleRefresh(index, response.status)
      if (!options.silent) input.notifySuccess('GPTMail 状态已更新')
    } catch (error: any) {
      const message = error?.message || '获取 GPTMail 状态失败'
      setState(index, { loading: false, error: message, data: previous })
      if (!options.silent) input.notifyError(message)
    }
  }

  async function refreshPublicKey(index: number, provider: RegisterProvider, options: GptMailCheckOptions = {}) {
    const previous = state(index).data
    try {
      const response = await registerApi.refreshGptMailKey(sanitizedProviderPayload(provider), options.force ?? true)
      setState(index, { loading: false, error: '', data: response.status })
      if (options.reschedule !== false) scheduleRefresh(index, response.status)
    } catch (error: any) {
      const message = error?.message || '刷新 GPTMail 公共 Key 失败'
      setState(index, { loading: false, error: message, data: previous })
    }
  }

  function startClock() {
    stopClock()
    clockNow.value = Date.now()
    input.runtime.setInterval(CLOCK_INTERVAL_KEY, 10_000, () => {
      clockNow.value = Date.now()
    })
  }

  function stopClock() {
    input.runtime.clearInterval(CLOCK_INTERVAL_KEY)
  }

  return {
    statusStates,
    state,
    clearState,
    clearAllStates,
    clearAllRefreshTimers,
    pruneStates,
    statusByIndex,
    statusBusy,
    statusTone,
    statusTitle,
    remainingText,
    resetText,
    statusHint,
    checkStatus,
    refreshPublicKey,
    startClock,
    stopClock,
  }
}
