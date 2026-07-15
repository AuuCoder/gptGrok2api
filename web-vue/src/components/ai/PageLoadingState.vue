<template>
  <StateBlock :compact="compact" :dashed="dashed" :title="title" :description="description">
    <template #media>
      <span class="page-loading-state__spinner" aria-hidden="true"></span>
    </template>
  </StateBlock>
</template>

<script setup lang="ts">
import StateBlock from './StateBlock.vue'

withDefaults(defineProps<{
  title?: string
  description?: string
  compact?: boolean
  dashed?: boolean
}>(), {
  title: '正在加载',
  description: '',
  compact: false,
  dashed: false,
})
</script>

<style scoped>
.page-loading-state__spinner {
  display: inline-block;
  width: 20px;
  height: 20px;
  border: 2px solid hsl(var(--border));
  border-top-color: hsl(var(--foreground));
  border-radius: 999px;
  animation: page-loading-state-spin 0.85s linear infinite;
}

@keyframes page-loading-state-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
