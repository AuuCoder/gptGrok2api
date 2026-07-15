import { computed, type Ref } from 'vue'
import type { RuntimeLog, RuntimeLogsResponse, SystemLogRow, SystemLogsResponse } from '@/api/logs'
import { saveBlob } from '@/lib/downloads'

export type LogExportView = 'system' | 'runtime'

export type LogExportRuntimeInput = {
  activeLogView: Ref<LogExportView>
  logs: Ref<SystemLogRow[]>
  runtimeLogs: Ref<RuntimeLog[]>
  logMeta: SystemLogsResponse
  runtimeMeta: RuntimeLogsResponse
  currentPage: Readonly<Ref<number>>
}

function exportTimestamp() {
  return new Date().toISOString().slice(0, 19).replace(/:/g, '-')
}

function saveJsonBlob(payload: unknown, filename: string) {
  const blob = new Blob(
    [JSON.stringify(payload, null, 2)],
    { type: 'application/json' },
  )
  saveBlob(blob, filename)
}

export function useLogExportRuntime(input: LogExportRuntimeInput) {
  const activeExportDisabled = computed(() => (
    input.activeLogView.value === 'runtime'
      ? input.runtimeLogs.value.length === 0
      : input.logs.value.length === 0
  ))

  function exportSystemLogs() {
    saveJsonBlob(
      {
        exported_at: new Date().toISOString(),
        page: input.currentPage.value,
        total: input.logMeta.total,
        logs: input.logs.value.map((item) => item.raw),
      },
      `logs_${exportTimestamp()}.json`,
    )
  }

  function exportRuntimeLogs() {
    saveJsonBlob(
      {
        exported_at: new Date().toISOString(),
        total: input.runtimeMeta.total,
        logs: input.runtimeLogs.value,
      },
      `runtime_logs_${exportTimestamp()}.json`,
    )
  }

  function exportActiveLogs() {
    if (input.activeLogView.value === 'runtime') {
      exportRuntimeLogs()
      return
    }
    exportSystemLogs()
  }

  return {
    activeExportDisabled,
    exportSystemLogs,
    exportRuntimeLogs,
    exportActiveLogs,
  }
}
