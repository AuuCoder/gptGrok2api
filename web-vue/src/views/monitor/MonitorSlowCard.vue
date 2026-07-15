<template>
  <div
    v-memo="[signature]"
    class="rounded-2xl border border-border bg-background px-3 py-3"
  >
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0">
        <p class="truncate text-sm font-medium text-foreground">
          {{ row.model || '-' }}
          <span class="font-mono text-xs text-muted-foreground">{{ shortCallId(row.call_id) }}</span>
        </p>
        <p class="mt-1 text-xs text-muted-foreground">{{ row.endpoint || '-' }}</p>
      </div>
      <StateBadge :tone="statusTone(row.status)" size="xs" shape="rounded" :bordered="false">
        {{ formatMs(row.duration_ms) }}
      </StateBadge>
    </div>
    <div class="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
      <span
        v-for="item in metricItems"
        :key="`${row.call_id}-${item.key}`"
        class="rounded-xl px-2 py-1"
        :class="item.important ? 'bg-primary/10 text-primary' : 'bg-muted/60'"
      >
        {{ item.label }} {{ item.value }}
      </span>
    </div>
    <p v-if="reason" class="mt-2 text-xs text-muted-foreground">
      {{ reason }}
    </p>
    <p v-if="row.error" class="mt-2 line-clamp-2 text-xs text-muted-foreground">
      {{ row.error }}
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { RealtimeMonitorRecord } from '@/api/monitor'
import StateBadge from '@/components/ai/StateBadge.vue'
import {
  formatMs,
  shortCallId,
  slowMetricItems,
  slowRowReason,
  statusTone,
} from '@/views/monitor/monitorView'

const props = defineProps<{
  row: RealtimeMonitorRecord
  signature: string
}>()

const metricItems = computed(() => slowMetricItems(props.row))
const reason = computed(() => slowRowReason(props.row))
</script>
