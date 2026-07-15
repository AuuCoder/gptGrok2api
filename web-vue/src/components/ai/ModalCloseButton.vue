<template>
  <button
    v-bind="$attrs"
    type="button"
    class="modal-close-button"
    :class="[
      `modal-close-button--${tone}`,
      `modal-close-button--${size}`,
    ]"
    :disabled="disabled"
    :aria-label="label"
    :title="label"
    @click="$emit('click', $event)"
  >
    <Icon icon="lucide:x" :class="size === 'sm' ? 'h-3.5 w-3.5' : 'h-4 w-4'" />
  </button>
</template>

<script setup lang="ts">
import { Icon } from '@iconify/vue'

defineOptions({
  inheritAttrs: false,
})

withDefaults(defineProps<{
  label?: string
  disabled?: boolean
  tone?: 'default' | 'dark'
  size?: 'sm' | 'md'
}>(), {
  label: '关闭',
  disabled: false,
  tone: 'default',
  size: 'md',
})

defineEmits<{
  click: [event: MouseEvent]
}>()
</script>

<style scoped>
.modal-close-button {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: hsl(var(--muted-foreground));
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s, opacity 0.15s;
}

.modal-close-button--md {
  width: 2rem;
  height: 2rem;
}

.modal-close-button--sm {
  width: 1.75rem;
  height: 1.75rem;
}

.modal-close-button--default:hover,
.modal-close-button--default:focus-visible {
  border-color: hsl(var(--border) / 0.72);
  background: hsl(var(--muted) / 0.72);
  color: hsl(var(--foreground));
}

.modal-close-button--dark {
  border-color: rgb(255 255 255 / 0.18);
  background: rgb(255 255 255 / 0.12);
  color: white;
}

.modal-close-button--dark:hover,
.modal-close-button--dark:focus-visible {
  border-color: rgb(255 255 255 / 0.32);
  background: rgb(255 255 255 / 0.22);
  color: white;
}

.modal-close-button:focus-visible {
  outline: 2px solid hsl(var(--primary) / 0.38);
  outline-offset: 2px;
}

.modal-close-button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}
</style>
