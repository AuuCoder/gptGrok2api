<template>
  <div
    v-if="totalCount > 0"
    class="flex flex-col gap-3 border-t border-border/60 pt-4 md:flex-row md:items-center md:justify-between"
  >
    <div class="text-xs text-muted-foreground">
      当前展示 {{ visibleCount }} / {{ totalCount }} {{ unit }}
    </div>
    <div class="flex flex-wrap items-center gap-2">
      <span class="text-xs text-muted-foreground">每页</span>
      <div class="w-[110px] shrink-0">
        <GroupedSelectMenu
          :model-value="String(pageSize)"
          :groups="pageSizeMenuGroups"
          :placement="placement"
          :aria-label="`${unit}每页数量`"
          @update:model-value="setPageSize"
        />
      </div>
      <Button
        size="sm"
        variant="outline"
        :disabled="disabled || safePage <= 1"
        @click="emit('update:page', safePage - 1)"
      >
        上一页
      </Button>
      <span class="text-sm text-muted-foreground tabular-nums">{{ safePage }} / {{ pageCount }}</span>
      <Button
        size="sm"
        variant="outline"
        :disabled="disabled || safePage >= pageCount"
        @click="emit('update:page', safePage + 1)"
      >
        下一页
      </Button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Button } from 'nanocat-ui'
import GroupedSelectMenu from '../ui/GroupedSelectMenu.vue'

type MenuPlacement = 'auto' | 'top' | 'bottom' | 'left' | 'right' | 'up' | 'down'

const props = withDefaults(defineProps<{
  page: number
  pageSize: number
  totalCount: number
  pageSizeOptions?: number[]
  unit?: string
  disabled?: boolean
  placement?: MenuPlacement
}>(), {
  pageSizeOptions: () => [20, 50, 100],
  unit: '条',
  disabled: false,
  placement: 'auto',
})

const emit = defineEmits<{
  (e: 'update:page', value: number): void
  (e: 'update:pageSize', value: number): void
}>()

const pageCount = computed(() => Math.max(1, Math.ceil(props.totalCount / Math.max(1, props.pageSize))))
const safePage = computed(() => Math.min(pageCount.value, Math.max(1, props.page)))
const startIndex = computed(() => (props.totalCount ? (safePage.value - 1) * props.pageSize + 1 : 0))
const endIndex = computed(() => Math.min(props.totalCount, safePage.value * props.pageSize))
const visibleCount = computed(() => (props.totalCount ? Math.max(0, endIndex.value - startIndex.value + 1) : 0))
const pageSizeMenuGroups = computed(() => [{
  options: props.pageSizeOptions.map((value) => ({
    label: `${value} / 页`,
    value: String(value),
  })),
}])

function setPageSize(value: string | string[]) {
  const rawValue = Array.isArray(value) ? value[0] : value
  const next = Number(rawValue)
  if (!Number.isFinite(next) || next <= 0) return
  emit('update:pageSize', next)
}
</script>
