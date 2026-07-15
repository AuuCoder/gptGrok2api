<template>
  <div v-if="imageUrls.length" class="log-image-preview-cell">
    <button
      type="button"
      class="log-image-preview-cell__button"
      title="查看图片预览"
      @click="$emit('preview-click')"
    >
      <img
        v-if="firstImageUrl && !firstImageBroken"
        :src="firstImageUrl"
        :alt="alt || '日志结果图片'"
        loading="lazy"
        class="log-image-preview-cell__image"
        @error="$emit('image-error', $event, firstImageUrl)"
      />
      <span v-else class="log-image-preview-cell__fallback">
        无法预览
      </span>
    </button>
    <MetaChip v-if="imageUrls.length > 1" size="xs" tone="muted">
      +{{ imageUrls.length - 1 }}
    </MetaChip>
  </div>
  <span v-else class="log-image-preview-cell__empty">-</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import MetaChip from './MetaChip.vue'

const props = defineProps<{
  imageUrls: string[]
  firstImageBroken?: boolean
  alt?: string
}>()

defineEmits<{
  (e: 'preview-click'): void
  (e: 'image-error', event: Event, url: string): void
}>()

const firstImageUrl = computed(() => props.imageUrls[0] || '')
</script>

<style scoped>
.log-image-preview-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}

.log-image-preview-cell__button {
  position: relative;
  height: 56px;
  width: 56px;
  overflow: hidden;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  background: hsl(var(--muted));
  font-size: 10px;
  color: hsl(var(--muted-foreground));
  transition: border-color 0.18s ease;
}

.log-image-preview-cell__button:hover {
  border-color: hsl(var(--primary) / 0.4);
}

.log-image-preview-cell__image {
  height: 100%;
  width: 100%;
  object-fit: cover;
}

.log-image-preview-cell__fallback {
  display: flex;
  height: 100%;
  width: 100%;
  align-items: center;
  justify-content: center;
  padding: 0 4px;
  text-align: center;
}

.log-image-preview-cell__empty {
  font-size: 12px;
  color: hsl(var(--muted-foreground));
}
</style>
