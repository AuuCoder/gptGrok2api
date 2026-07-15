<template>
  <SelectionBulkBar
    :selected-count="selectedCount"
    :summary-text="`已选择 ${selectedCount} 个账户`"
    max-width="48rem"
  >
    <FloatingActionMenu
      :label="busy ? (busyLabel || '批量处理中...') : `批量操作（${selectedCount}）`"
      :items="items"
      :disabled="busy"
      :trigger-min-width="124"
      align="right"
      @select="emit('select', $event)"
    />
    <Button
      size="xs"
      variant="outline"
      :disabled="busy"
      @click="emit('clear')"
    >
      取消选择
    </Button>
  </SelectionBulkBar>
</template>

<script setup lang="ts">
import { Button } from 'nanocat-ui'
import type { ActionMenuItem } from 'nanocat-ui'
import FloatingActionMenu from './FloatingActionMenu.vue'
import SelectionBulkBar from './SelectionBulkBar.vue'

type BulkActionMenuItem = ActionMenuItem & {
  children?: BulkActionMenuItem[]
}

defineProps<{
  selectedCount: number
  busy: boolean
  busyLabel?: string
  items: BulkActionMenuItem[]
}>()

const emit = defineEmits<{
  (e: 'select', key: string): void
  (e: 'clear'): void
}>()
</script>
