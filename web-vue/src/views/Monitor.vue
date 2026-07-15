<template>
  <div class="space-y-6">
    <PagePanel class="space-y-5">
      <PanelHeader title="实时监控" align="start">
        <template #copy>
          <p class="mt-1 text-xs text-muted-foreground">
            进程内实时窗口，用于观察入口、账号、出口、上游生成、断流和本地拒绝/繁忙。
          </p>
          <p class="mt-1 text-xs text-muted-foreground">
            入口排队高时关注 CHATGPT2API_THREAD_TOKENS；最近更新：{{ monitorData?.updated_at || '未获取' }}
          </p>
        </template>
        <template #actions>
          <StateBadge :tone="autoRefresh ? 'success' : 'muted'" shape="rounded">
            {{ autoRefresh ? '自动刷新' : '已暂停' }}
          </StateBadge>
          <label class="flex items-center gap-2 text-xs text-muted-foreground">
            <span class="whitespace-nowrap">间隔</span>
            <Input
              :model-value="String(refreshIntervalSeconds)"
              type="number"
              min="1"
              max="300"
              step="1"
              root-class="w-16"
              @update:model-value="setRefreshIntervalInput"
              @blur="applyRefreshInterval()"
              @change="applyRefreshInterval()"
              @keyup.enter="applyRefreshInterval()"
            />
            <span class="whitespace-nowrap">秒</span>
          </label>
          <Button size="sm" variant="outline" :disabled="isLoading" @click="loadMonitor(false)">
            {{ isLoading ? '刷新中...' : '立即刷新' }}
          </Button>
          <Button size="sm" variant="outline" @click="toggleAutoRefresh">
            {{ autoRefresh ? '暂停刷新' : '继续刷新' }}
          </Button>
        </template>
      </PanelHeader>

      <div class="grid gap-3 xl:grid-cols-2">
        <div
          v-for="group in diagnosticGroups"
          :key="group.key"
          class="monitor-metric-group"
        >
          <div class="flex items-center justify-between gap-3">
            <p class="text-sm font-semibold text-foreground">{{ group.title }}</p>
            <p class="text-xs text-muted-foreground">{{ group.meta }}</p>
          </div>
          <div class="mt-3 grid gap-2 sm:grid-cols-2 2xl:grid-cols-4">
            <div
              v-for="item in group.items"
              :key="`${group.key}-${item.key}`"
              class="monitor-metric-cell"
            >
              <p class="truncate text-xs text-muted-foreground">{{ item.label }}</p>
              <p
                class="mt-1 text-base font-semibold leading-none tabular-nums"
                :class="item.value === '-' ? 'text-muted-foreground' : item.valueClass"
              >
                {{ item.value }}
              </p>
              <p v-if="item.meta" class="mt-1 truncate text-[11px] text-muted-foreground">
                {{ item.meta }}
              </p>
            </div>
          </div>
        </div>
      </div>

      <StateBlock
        v-if="loadError"
        compact
        dashed
        title="实时监控加载失败"
        :description="loadError"
      />
    </PagePanel>

    <PagePanel flush>
      <div class="p-4">
        <PanelHeader title="活跃请求" align="start">
          <template #copy>
            <p class="mt-1 text-xs text-muted-foreground">
              正在运行的图片请求，按进入时间排序。
            </p>
          </template>
          <template #actions>
            <MetaChip size="xs" tone="info">当前并发 {{ activeRows.length }} / {{ threadTokens }}</MetaChip>
            <MetaChip size="xs" tone="muted">入口排队 {{ entryQueueText }}</MetaChip>
          </template>
        </PanelHeader>
      </div>
      <div v-if="activeStageItems.length" class="flex flex-wrap gap-2 px-4 pb-3">
        <MetaChip
          v-for="item in activeStageItems"
          :key="item.label"
          size="xs"
          tone="muted"
        >
          {{ item.label }} {{ item.count }}
        </MetaChip>
      </div>
      <TableShell v-if="activeRows.length">
        <table class="monitor-table">
          <thead>
            <tr>
              <th>请求</th>
              <th>模型</th>
              <th>阶段</th>
              <th>已耗时</th>
              <th>关键耗时</th>
              <th>出口</th>
              <th>账号</th>
            </tr>
          </thead>
          <tbody>
            <MonitorActiveRow
              v-for="row in activeRows"
              :key="row.call_id"
              :row="row"
              :signature="activeRowSignature(row)"
            />
          </tbody>
        </table>
      </TableShell>
      <div v-else class="px-4 pb-4">
        <StateBlock compact dashed title="暂无活跃请求" description="开始压测或发起图片请求后，这里会实时出现运行中的调用。" />
      </div>
    </PagePanel>

    <div class="grid items-stretch gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(22rem,0.72fr)]">
      <PagePanel flush class="monitor-paired-panel">
        <div class="monitor-paired-header">
          <div class="monitor-paired-header__copy">
            <p class="ui-section-title">最近完成</p>
            <p class="monitor-paired-header__desc">
              最近完成的图片相关调用，窗口保存在进程内存中。
            </p>
          </div>
          <div class="monitor-paired-header__meta">
            <MetaChip size="xs" tone="muted">{{ completedWindowText }}</MetaChip>
          </div>
        </div>
        <div v-if="recentRows.length" class="monitor-paired-body">
          <TableShell>
            <table class="monitor-table">
              <thead>
                <tr>
                  <th>请求</th>
                  <th>状态</th>
                  <th>模型</th>
                  <th>总耗时</th>
                  <th>入口等待</th>
                  <th>账号 / 出口</th>
                </tr>
              </thead>
              <tbody>
                <MonitorRecentRow
                  v-for="row in recentRows"
                  :key="`recent-${row.call_id}-${row.ended_at}`"
                  :row="row"
                  :signature="recentRowSignature(row)"
                />
              </tbody>
            </table>
          </TableShell>
        </div>
        <div v-else class="monitor-paired-body px-4 pb-4">
          <StateBlock compact dashed title="暂无完成记录" description="当前容器启动后还没有图片相关请求完成。" />
        </div>
      </PagePanel>

      <PagePanel flush class="monitor-paired-panel">
        <div class="monitor-paired-header">
          <div class="monitor-paired-header__copy">
            <p class="ui-section-title">慢请求</p>
            <p class="monitor-paired-header__desc">
              按等待入口、等待账号、等待出口、上游生成和上游断流综合排序。
            </p>
          </div>
          <div class="monitor-paired-header__meta">
            <MetaChip size="xs" tone="muted">慢 {{ slowRows.length }}</MetaChip>
          </div>
        </div>
        <div v-if="slowRows.length" class="monitor-paired-body space-y-2 px-4 pb-4">
          <MonitorSlowCard
            v-for="row in slowRows"
            :key="`slow-${row.call_id}-${row.ended_at}`"
            :row="row"
            :signature="slowRowSignature(row)"
          />
        </div>
        <div v-else class="monitor-paired-body px-4 pb-4">
          <StateBlock compact dashed title="暂无慢请求" description="窗口内没有可排序的完成请求。" />
        </div>
      </PagePanel>
    </div>

    <PagePanel flush>
      <div class="p-4">
        <PanelHeader title="阶段事件" align="start">
          <template #copy>
            <p class="mt-1 text-xs text-muted-foreground">
              最近阶段变化，辅助观察请求是否集中卡在同一环节。
            </p>
          </template>
        </PanelHeader>
      </div>
      <TableShell v-if="eventRows.length">
        <table class="monitor-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>请求</th>
              <th>模型</th>
              <th>阶段</th>
              <th>耗时</th>
            </tr>
          </thead>
          <tbody>
            <MonitorEventRow
              v-for="(row, index) in eventRows"
              :key="`${row.call_id}-${row.event}-${index}`"
              :row="row"
              :signature="eventRowSignature(row, index)"
            />
          </tbody>
        </table>
      </TableShell>
      <div v-else class="px-4 pb-4">
        <StateBlock compact dashed title="暂无阶段事件" description="有图片请求进入后会开始记录。" />
      </div>
    </PagePanel>

  </div>
</template>

<script setup lang="ts">
import { computed, ref, shallowRef } from 'vue'
import { Button, Input } from 'nanocat-ui'
import { monitorApi, type RealtimeMonitorResponse } from '@/api/monitor'
import MetaChip from '@/components/ai/MetaChip.vue'
import PagePanel from '@/components/ai/PagePanel.vue'
import PanelHeader from '@/components/ai/PanelHeader.vue'
import StateBadge from '@/components/ai/StateBadge.vue'
import StateBlock from '@/components/ai/StateBlock.vue'
import TableShell from '@/components/ai/TableShell.vue'
import { usePageQuery, useSerialVisibilityPolling } from '@/composables/usePageQuery'
import { usePageRuntime } from '@/composables/usePageRuntime'
import MonitorActiveRow from '@/views/monitor/MonitorActiveRow.vue'
import MonitorEventRow from '@/views/monitor/MonitorEventRow.vue'
import MonitorRecentRow from '@/views/monitor/MonitorRecentRow.vue'
import MonitorSlowCard from '@/views/monitor/MonitorSlowCard.vue'
import {
  activeRowSignature,
  activeStageItems as buildActiveStageItems,
  buildDiagnosticGroups,
  completedWindowText as buildCompletedWindowText,
  entryQueueText as buildEntryQueueText,
  eventRowSignature,
  recentRowSignature,
  slowRowSignature,
} from '@/views/monitor/monitorView'

defineOptions({ name: 'Monitor' })

const monitorData = shallowRef<RealtimeMonitorResponse | null>(null)
const isLoading = ref(false)
const loadError = ref('')
const autoRefresh = ref(true)
const REFRESH_INTERVAL_STORAGE_KEY = 'chatgpt2api_monitor_refresh_interval_secs'
const DEFAULT_REFRESH_INTERVAL_SECONDS = 5
const MIN_REFRESH_INTERVAL_SECONDS = 1
const MAX_REFRESH_INTERVAL_SECONDS = 300
const refreshIntervalSeconds = ref(readStoredRefreshInterval())
const pageRuntime = usePageRuntime('monitor')
const REFRESH_REQUEST_KEY = 'monitor:refresh'
const POLL_TIMER_KEY = 'monitor:poll'
const monitorQuery = usePageQuery({
  runtime: pageRuntime,
  key: REFRESH_REQUEST_KEY,
  loading: isLoading,
  error: loadError,
  errorMessage: 'Request failed',
})
const monitorPolling = useSerialVisibilityPolling({
  runtime: pageRuntime,
  key: POLL_TIMER_KEY,
  intervalMs: () => normalizedRefreshIntervalSeconds() * 1000,
  enabled: () => autoRefresh.value,
  action: () => loadMonitor(true, 'auto'),
})

const summary = computed(() => monitorData.value?.summary)
const activeRows = computed(() => monitorData.value?.active || [])
const recentRows = computed(() => monitorData.value?.recent.slice(0, 20) || [])
const slowRows = computed(() => monitorData.value?.slow.slice(0, 8) || [])
const eventRows = computed(() => monitorData.value?.events.slice(0, 30) || [])
const threadTokens = computed(() => monitorData.value?.threadpool?.tokens || '-')
const completedWindowText = computed(() => buildCompletedWindowText(monitorData.value?.window))
const activeStageItems = computed(() => buildActiveStageItems(summary.value))

const entryQueueText = computed(() => buildEntryQueueText(summary.value))
const diagnosticGroups = computed(() => buildDiagnosticGroups(summary.value, threadTokens.value, completedWindowText.value))

async function loadMonitor(silent = true, source: 'auto' | 'manual' = silent ? 'auto' : 'manual') {
  const autoRequest = source === 'auto'
  if (autoRequest && (!pageRuntime.canRun.value || !autoRefresh.value)) return
  if (isLoading.value && silent) return
  await monitorQuery.run(
    () => monitorApi.realtime(),
    {
      apply: (data) => {
        if (autoRequest && !autoRefresh.value) return
        monitorData.value = data
        loadError.value = ''
      },
      silentLoading: silent,
    },
  )
}

function startPolling() {
  monitorPolling.start()
}

function stopPolling() {
  monitorPolling.stop()
  monitorQuery.invalidate()
}

function activateMonitor(refresh = false) {
  if (refresh) {
    void loadMonitor(false, 'manual')
  }
  startPolling()
}

function deactivateMonitor() {
  isLoading.value = false
  stopPolling()
}

function handleVisibilityChange() {
  startPolling()
  if (autoRefresh.value) {
    void loadMonitor(true, 'auto')
  }
}

function toggleAutoRefresh() {
  autoRefresh.value = !autoRefresh.value
  if (autoRefresh.value) {
    applyRefreshInterval(false)
    startPolling()
    void loadMonitor(true, 'auto')
  } else {
    stopPolling()
  }
}

function clampRefreshInterval(value: unknown) {
  const seconds = Math.round(Number(value || DEFAULT_REFRESH_INTERVAL_SECONDS))
  if (!Number.isFinite(seconds)) return DEFAULT_REFRESH_INTERVAL_SECONDS
  return Math.min(MAX_REFRESH_INTERVAL_SECONDS, Math.max(MIN_REFRESH_INTERVAL_SECONDS, seconds))
}

function readStoredRefreshInterval() {
  try {
    return clampRefreshInterval(window.localStorage.getItem(REFRESH_INTERVAL_STORAGE_KEY))
  } catch {
    return DEFAULT_REFRESH_INTERVAL_SECONDS
  }
}

function normalizedRefreshIntervalSeconds() {
  return clampRefreshInterval(refreshIntervalSeconds.value)
}

function setRefreshIntervalInput(value: unknown) {
  refreshIntervalSeconds.value = clampRefreshInterval(value)
}

function applyRefreshInterval(restart = true) {
  const nextValue = normalizedRefreshIntervalSeconds()
  refreshIntervalSeconds.value = nextValue
  try {
    window.localStorage.setItem(REFRESH_INTERVAL_STORAGE_KEY, String(nextValue))
  } catch {
    // ignore storage errors
  }
  if (restart && autoRefresh.value) {
    startPolling()
  }
}

pageRuntime.onActivate(({ initial }) => {
  activateMonitor(true)
})

pageRuntime.onShow(() => {
  handleVisibilityChange()
})

pageRuntime.onHide(() => {
  deactivateMonitor()
})

pageRuntime.onDeactivate(() => {
  deactivateMonitor()
})
</script>

<style scoped>
.monitor-table {
  width: 100%;
  min-width: 840px;
  border-collapse: collapse;
  text-align: left;
  font-size: 13px;
}

.monitor-table th,
.monitor-table :deep(th) {
  border-bottom: 1px solid hsl(var(--border));
  background: hsl(var(--muted) / 0.42);
  padding: 10px 14px;
  color: hsl(var(--muted-foreground));
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
}

.monitor-table td,
.monitor-table :deep(td) {
  border-bottom: 1px solid hsl(var(--border) / 0.72);
  padding: 12px 14px;
  vertical-align: top;
  color: hsl(var(--foreground));
  line-height: 1.45;
}

.monitor-table :deep(td) {
  height: 3.75rem;
}

.monitor-table :deep(td:nth-child(1)) {
  min-width: 8.75rem;
}

.monitor-table :deep(td:nth-child(2)),
.monitor-table :deep(td:nth-child(3)),
.monitor-table :deep(td:nth-child(4)) {
  white-space: nowrap;
}

.monitor-table :deep(td:nth-child(5)),
.monitor-table :deep(td:nth-child(6)),
.monitor-table :deep(td:nth-child(7)) {
  max-width: 18rem;
  white-space: normal;
  overflow-wrap: anywhere;
}

.monitor-table tbody tr:hover td,
.monitor-table :deep(tbody tr:hover td) {
  background: hsl(var(--muted) / 0.28);
}

.monitor-metric-group {
  border-radius: 16px;
  border: 1px solid hsl(var(--border));
  background: hsl(var(--background));
  padding: 14px;
}

.monitor-metric-cell {
  min-width: 0;
  border-radius: 12px;
  background: hsl(var(--muted) / 0.34);
  padding: 10px 12px;
}

.monitor-paired-panel {
  display: flex;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  flex-direction: column;
}

.monitor-paired-header {
  display: flex;
  min-height: 92px;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 20px 14px;
}

.monitor-paired-header__copy {
  min-width: 0;
  flex: 1 1 auto;
}

.monitor-paired-header__desc {
  margin-top: 6px;
  max-width: 48rem;
  color: hsl(var(--muted-foreground));
  font-size: 12px;
  line-height: 1.55;
}

.monitor-paired-header__meta {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: flex-end;
  padding-top: 2px;
}

.monitor-paired-body {
  height: clamp(360px, calc(100vh - 350px), 560px);
  min-height: 0;
  overflow-y: auto;
}

@media (max-width: 768px) {
  .monitor-paired-header {
    min-height: auto;
    flex-direction: column;
    align-items: stretch;
  }

  .monitor-paired-header__meta {
    justify-content: flex-start;
    padding-top: 0;
  }

}

</style>
