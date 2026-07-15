<template>
  <div class="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
    <Checkbox
      :model-value="allSelected"
      @update:model-value="emit('toggle-all', $event)"
    >
      全选当前结果
    </Checkbox>
    <MetaChip>
      账号总数 {{ totalCount }}
    </MetaChip>
    <MetaChip>
      已选 {{ selectedCount }}
    </MetaChip>
    <ViewModeSwitch
      :model-value="viewMode"
      list-label="列表视图"
      cards-label="卡片视图"
      @update:model-value="emit('update:viewMode', $event)"
    />
  </div>
</template>

<script setup lang="ts">
import { Checkbox, ViewModeSwitch } from 'nanocat-ui'
import MetaChip from './MetaChip.vue'

defineProps<{
  allSelected: boolean
  totalCount: number
  selectedCount: number
  viewMode: 'list' | 'cards'
}>()

const emit = defineEmits<{
  (e: 'toggle-all', value: boolean): void
  (e: 'update:viewMode', value: 'list' | 'cards'): void
}>()
</script>
