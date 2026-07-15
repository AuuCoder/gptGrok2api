import { computed, reactive, ref, watch, type Ref } from 'vue'

import { logsApi, type RuntimeLog, type RuntimeLogsResponse } from '@/api/logs'
import type { PageRuntime } from '@/composables/usePageRuntime'
import { usePageDebouncedAction, usePageQuery, useSerialVisibilityPolling } from '@/composables/usePageQuery'
import { getNumberPreference, preferenceKeys, setNumberPreference } from '@/lib/preferences'
import {
  activeRuntimeFilterCount,
  buildRuntimeFilterMenuItems,
  formatRuntimeLogLine,
  normalizeRuntimeLimit,
  parseRuntimeFilterMenuKey,
  runtimeMetricItems,
} from '@/views/logs/logsView'

type LogRuntimeQueryInput = {
  runtime: PageRuntime
  activeLogView: Ref<'system' | 'runtime'>
  notifyError: (message: string) => void
}

type RuntimeFetchOptions = {
  silentError?: boolean
  silentLoading?: boolean
}

const DEFAULT_RUNTIME_LOG_LIMIT = 500
const RUNTIME_LOGS_REQUEST_KEY = 'logs:runtime'
const AUTO_REFRESH_TIMER_KEY = 'logs:auto-refresh'
const RUNTIME_FILTER_FETCH_TIMER_KEY = 'logs:runtime-filter-fetch'

export function useLogRuntimeQueryRuntime(input: LogRuntimeQueryInput) {
  const runtimeLogs = ref<RuntimeLog[]>([])
  const runtimeFetching = ref(false)
  const runtimeLoadError = ref('')
  const autoRefreshEnabled = ref(false)
  const runtimeFilters = reactive({
    level: '',
    source: '',
    search: '',
    limit: DEFAULT_RUNTIME_LOG_LIMIT,
  })
  const runtimeMeta = reactive<RuntimeLogsResponse>({
    items: [],
    total: 0,
    limit: DEFAULT_RUNTIME_LOG_LIMIT,
    sources: {
      memory: true,
      files: [],
    },
  })

  const runtimeLogsQuery = usePageQuery({
    runtime: input.runtime,
    key: RUNTIME_LOGS_REQUEST_KEY,
    loading: runtimeFetching,
    error: runtimeLoadError,
    errorMessage: '运行日志加载失败',
  })

  const runtimeFilterFetch = usePageDebouncedAction({
    runtime: input.runtime,
    key: RUNTIME_FILTER_FETCH_TIMER_KEY,
    delayMs: 250,
    shouldRun: () => input.activeLogView.value === 'runtime',
    action: () => {
      void fetchRuntimeLogs()
    },
  })

  const runtimeAutoRefresh = useSerialVisibilityPolling({
    runtime: input.runtime,
    key: AUTO_REFRESH_TIMER_KEY,
    intervalMs: 8000,
    enabled: () => autoRefreshEnabled.value && input.activeLogView.value === 'runtime',
    action: () => fetchRuntimeLogs({ silentError: true, silentLoading: true }),
  })

  const metricItems = computed(() => runtimeMetricItems(runtimeLogs.value))
  const rawText = computed(() => runtimeLogs.value.map(formatRuntimeLogLine).join('\n'))
  const activeFilterCount = computed(() => activeRuntimeFilterCount(runtimeFilters))
  const filterLabel = computed(() => activeFilterCount.value ? `筛选 ${activeFilterCount.value}` : '筛选')
  const filterMenuItems = computed(() => buildRuntimeFilterMenuItems(runtimeFilters))

  function applyResponse(response: RuntimeLogsResponse) {
    runtimeLogs.value = response.items
    runtimeMeta.items = response.items
    runtimeMeta.total = response.total
    runtimeMeta.limit = response.limit
    runtimeMeta.sources = response.sources
  }

  function updateLimit(value: string) {
    runtimeFilters.limit = normalizeRuntimeLimit(value, DEFAULT_RUNTIME_LOG_LIMIT)
  }

  function loadStoredLimit() {
    runtimeFilters.limit = getNumberPreference(preferenceKeys.runtimeLogLimit, DEFAULT_RUNTIME_LOG_LIMIT, { min: 1, max: 2000 })
  }

  function handleFilterMenuSelect(key: string) {
    const selection = parseRuntimeFilterMenuKey(key)
    if (!selection) return
    runtimeFilters[selection.key] = selection.value
  }

  async function fetchRuntimeLogs(options: RuntimeFetchOptions = {}) {
    await runtimeLogsQuery.run(
      () => logsApi.listRuntime({
        level: runtimeFilters.level || undefined,
        source: runtimeFilters.source || undefined,
        search: runtimeFilters.search || undefined,
        limit: runtimeFilters.limit,
      }),
      {
        apply: applyResponse,
        onError: (message) => {
          if (options.silentError) return
          input.notifyError(message)
        },
        silentError: options.silentError,
        silentLoading: options.silentLoading,
      },
    )
  }

  function scheduleAutoRefresh() {
    if (!input.runtime.canRun.value || !autoRefreshEnabled.value || input.activeLogView.value !== 'runtime') {
      runtimeAutoRefresh.stop()
      return
    }
    runtimeAutoRefresh.start()
  }

  function toggleAutoRefresh() {
    autoRefreshEnabled.value = !autoRefreshEnabled.value
    scheduleAutoRefresh()
  }

  function clearTimers() {
    runtimeAutoRefresh.stop()
    runtimeFilterFetch.clear()
  }

  function invalidate() {
    runtimeLogsQuery.invalidate()
  }

  function deactivate() {
    invalidate()
    clearTimers()
  }

  function activateViewIfNeeded() {
    if (input.activeLogView.value !== 'runtime') return
    if (runtimeLogs.value.length === 0 && !runtimeLoadError.value) {
      void fetchRuntimeLogs()
    }
  }

  function handleShow() {
    scheduleAutoRefresh()
    if (input.activeLogView.value === 'runtime' && autoRefreshEnabled.value) {
      void fetchRuntimeLogs()
    }
  }

  watch(
    () => runtimeFilters.limit,
    (limit) => {
      setNumberPreference(preferenceKeys.runtimeLogLimit, limit)
    },
  )

  watch(autoRefreshEnabled, scheduleAutoRefresh)

  watch(input.activeLogView, () => {
    if (!input.runtime.canRun.value) return
    scheduleAutoRefresh()
  })

  watch(
    () => [
      runtimeFilters.level,
      runtimeFilters.source,
      runtimeFilters.search,
      runtimeFilters.limit,
    ],
    () => {
      runtimeFilterFetch.schedule()
    },
  )

  return {
    logs: runtimeLogs,
    fetching: runtimeFetching,
    loadError: runtimeLoadError,
    filters: runtimeFilters,
    meta: runtimeMeta,
    autoRefreshEnabled,
    metricItems,
    rawText,
    activeFilterCount,
    filterLabel,
    filterMenuItems,
    updateLimit,
    loadStoredLimit,
    handleFilterMenuSelect,
    fetch: fetchRuntimeLogs,
    scheduleAutoRefresh,
    toggleAutoRefresh,
    clearTimers,
    invalidate,
    deactivate,
    activateViewIfNeeded,
    handleShow,
  }
}
