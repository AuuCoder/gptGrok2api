<template>
  <tr v-memo="[signature]">
    <td>{{ row.time || '-' }}</td>
    <td class="font-mono text-xs">{{ shortCallId(row.call_id) }}</td>
    <td class="max-w-[14rem] truncate">{{ row.model || '-' }}</td>
    <td>{{ row.label || row.event }}</td>
    <td>{{ eventMetricText(row) }}</td>
  </tr>
</template>

<script setup lang="ts">
import type { RealtimeMonitorEvent } from '@/api/monitor'
import {
  eventMetricText,
  shortCallId,
} from '@/views/monitor/monitorView'

defineProps<{
  row: RealtimeMonitorEvent
  signature: string
}>()
</script>
