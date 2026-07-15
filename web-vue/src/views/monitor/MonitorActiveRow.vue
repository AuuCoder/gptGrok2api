<template>
  <tr v-memo="[signature]">
    <td>
      <p class="font-mono text-xs text-foreground">{{ shortCallId(row.call_id) }}</p>
      <p class="mt-1 text-[11px] text-muted-foreground">{{ row.endpoint || '-' }}</p>
    </td>
    <td>
      <MetaChip size="xs" tone="muted">{{ row.model || '-' }}</MetaChip>
    </td>
    <td>
      <StateBadge tone="info" shape="rounded" :bordered="false">
        {{ row.stage_label || row.stage || '运行中' }}
      </StateBadge>
    </td>
    <td>{{ formatMs(row.elapsed_ms) }}</td>
    <td>{{ metricDigest(row) }}</td>
    <td>
      <MetaChip size="xs" tone="muted">{{ egressText(row) }}</MetaChip>
    </td>
    <td class="max-w-[12rem] truncate">{{ row.account_email || '-' }}</td>
  </tr>
</template>

<script setup lang="ts">
import type { RealtimeMonitorRecord } from '@/api/monitor'
import MetaChip from '@/components/ai/MetaChip.vue'
import StateBadge from '@/components/ai/StateBadge.vue'
import {
  egressText,
  formatMs,
  metricDigest,
  shortCallId,
} from '@/views/monitor/monitorView'

defineProps<{
  row: RealtimeMonitorRecord
  signature: string
}>()
</script>
