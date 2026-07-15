<template>
  <tr
    v-memo="[signature]"
    class="border-t border-border transition-colors hover:bg-muted/30"
    :class="{ 'bg-primary/5': selected }"
  >
    <td class="py-4 pl-4 pr-2 align-middle">
      <Checkbox
        :model-value="selected"
        @update:model-value="handleSelect"
      >
        <span class="sr-only">选择日志 {{ item.time || item.id }}</span>
      </Checkbox>
    </td>
    <td class="py-4 pr-5 align-middle text-xs text-muted-foreground">
      <p class="whitespace-nowrap text-foreground">{{ item.time || '-' }}</p>
    </td>
    <td class="py-4 pr-5 align-middle">
      <MetaChip size="xs" tone="muted">{{ typeLabel(item.type) }}</MetaChip>
    </td>
    <td class="py-4 pr-5 align-middle">
      <p class="max-w-[12rem] truncate text-xs text-foreground" :title="token">
        {{ token || '-' }}
      </p>
    </td>
    <td class="py-4 pr-5 align-middle">
      <p class="max-w-[13rem] truncate text-xs text-foreground" :title="accountTitle">
        {{ account || '-' }}
      </p>
    </td>
    <td class="py-4 pr-5 align-middle text-xs text-muted-foreground">
      {{ formatDuration(item.durationMs) || '-' }}
    </td>
    <td class="py-4 pr-5 align-middle">
      <StateBadge :tone="statusTone(item)" shape="rounded" :bordered="false">
        {{ statusLabel(item) }}
      </StateBadge>
    </td>
    <td class="py-4 pr-5 align-middle">
      <LogImagePreviewCell
        :image-urls="item.imageUrls"
        :first-image-broken="firstImageBroken"
        :alt="item.preview || '日志结果图片'"
        @preview-click="handleOpenDetail"
        @image-error="handleImageError"
      />
    </td>
    <td class="py-4 pr-5 align-middle">
      <p
        class="max-w-[28rem] truncate text-xs text-foreground"
        :class="{ 'text-rose-600': failed }"
        :title="summary"
      >
        {{ summary || '-' }}
      </p>
    </td>
    <td class="py-4 pr-4 text-right align-middle">
      <div class="flex justify-end gap-1.5">
        <Button size="xs" variant="outline" @click="handleOpenDetail">
          查看详情
        </Button>
        <Button
          size="xs"
          variant="ghost"
          root-class="text-rose-600 hover:text-rose-700"
          @click="handleRequestDelete"
        >
          删除
        </Button>
      </div>
    </td>
  </tr>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Button, Checkbox } from 'nanocat-ui'
import LogImagePreviewCell from '@/components/ai/LogImagePreviewCell.vue'
import MetaChip from '@/components/ai/MetaChip.vue'
import StateBadge from '@/components/ai/StateBadge.vue'
import {
  formatLogDuration as formatDuration,
  isSystemLogFailed as isFailed,
  type SystemLogRow,
} from '@/api/logs'
import {
  statusLabel,
  statusTone,
  summaryText,
  tokenLabel,
  typeLabel,
} from '@/views/logs/logsView'

const props = defineProps<{
  item: SystemLogRow
  signature: string
  selected: boolean
  firstImageBroken: boolean
}>()

const emit = defineEmits<{
  (e: 'toggle-selection', id: string, checked: boolean): void
  (e: 'open-detail', item: SystemLogRow): void
  (e: 'request-delete-log', item: SystemLogRow): void
  (e: 'image-error', url: string): void
}>()

const token = computed(() => tokenLabel(props.item))
const account = computed(() => props.item.accountEmail || props.item.accountId)
const accountTitle = computed(() => [props.item.accountEmail, props.item.accountId].filter(Boolean).join(' / '))
const summary = computed(() => summaryText(props.item))
const failed = computed(() => isFailed(props.item))

function handleSelect(checked: boolean | string | number) {
  emit('toggle-selection', props.item.id, Boolean(checked))
}

function handleOpenDetail() {
  emit('open-detail', props.item)
}

function handleRequestDelete() {
  emit('request-delete-log', props.item)
}

function handleImageError(url: string) {
  emit('image-error', url)
}
</script>
