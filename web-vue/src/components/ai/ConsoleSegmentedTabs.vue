<template>
  <div class="console-segmented-tabs" :class="`console-segmented-tabs--${fit}`">
    <SegmentedTabs
      :model-value="modelValue"
      :options="options"
      :aria-label="ariaLabel"
      @update:model-value="(value) => emit('update:modelValue', value)"
    />
  </div>
</template>

<script setup lang="ts">
import { SegmentedTabs } from 'nanocat-ui'
import type { SegmentedOption, SegmentedValue } from 'nanocat-ui'

withDefaults(defineProps<{
  modelValue: SegmentedValue
  options: SegmentedOption[]
  ariaLabel?: string
  fit?: 'stretch' | 'content'
}>(), {
  fit: 'stretch',
})

const emit = defineEmits<{
  'update:modelValue': [value: SegmentedValue]
}>()
</script>

<style scoped>
.console-segmented-tabs {
  display: flex;
  width: 100%;
}

.console-segmented-tabs--content {
  width: max-content;
  max-width: 100%;
}

.console-segmented-tabs :deep(.ui-segmented) {
  width: 100%;
  flex-wrap: wrap;
  gap: 4px;
  border-color: hsl(var(--border));
  border-radius: 999px;
  background: hsl(var(--muted) / 0.3);
  padding: 4px;
}

.console-segmented-tabs--content :deep(.ui-segmented) {
  width: auto;
}

.console-segmented-tabs :deep(.ui-segmented-btn) {
  min-height: 32px;
  border-radius: 999px;
  padding: 0 12px;
  font-size: 12px;
  line-height: 1;
  font-weight: 500;
  white-space: nowrap;
}

.console-segmented-tabs--stretch :deep(.ui-segmented-btn) {
  flex: 1 1 0;
  justify-content: center;
}

.console-segmented-tabs--content :deep(.ui-segmented-btn) {
  flex: 0 0 auto;
}

.console-segmented-tabs :deep(.ui-segmented-btn:hover:not(.ui-segmented-btn-active)) {
  background: hsl(var(--accent) / 0.5);
  color: hsl(var(--foreground));
}

.console-segmented-tabs :deep(.ui-segmented-btn-active) {
  border-color: hsl(var(--border));
  background: hsl(var(--card));
  color: hsl(var(--foreground));
  box-shadow: 0 1px 2px rgb(15 23 42 / 0.06);
}
</style>
