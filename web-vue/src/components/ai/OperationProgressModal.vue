<template>
  <ModalShell :open="open" :max-width="maxWidth" :z-index="zIndex" panel-class="p-6">
    <ModalHeader
      :title="title"
      :subtitle="subtitle"
      :close-disabled="busy"
      :bordered="false"
      flush
      @close="$emit('close')"
    />

    <div class="mt-5 space-y-4">
      <div class="grid grid-cols-2 gap-2">
        <div class="operation-progress-card">
          <span>总数</span>
          <strong>{{ total }}</strong>
        </div>
        <div class="operation-progress-card">
          <span>{{ statusLabel }}</span>
          <strong>{{ current }}</strong>
        </div>
      </div>

      <ProgressBar :value="progressValue" :aria-label="title" />

      <p v-if="message" class="text-sm text-muted-foreground">{{ message }}</p>
      <p v-if="error" class="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
        {{ error }}
      </p>
      <div v-if="$slots.details" class="operation-progress-details">
        <slot name="details" />
      </div>
    </div>

    <ModalFooter v-if="busy && canCancel" class="mt-6" :bordered="false" flush>
      <Button v-if="busy && canCancel" size="sm" variant="outline" @click="$emit('cancel')">停止</Button>
    </ModalFooter>
  </ModalShell>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Button } from 'nanocat-ui'
import ModalFooter from './ModalFooter.vue'
import ModalHeader from './ModalHeader.vue'
import ModalShell from './ModalShell.vue'
import ProgressBar from './ProgressBar.vue'

const props = withDefaults(defineProps<{
  open: boolean
  title: string
  subtitle?: string
  total?: number
  current?: number
  statusLabel?: string
  message?: string
  error?: string
  busy?: boolean
  canCancel?: boolean
  zIndex?: number
  maxWidth?: string
}>(), {
  subtitle: '',
  total: 0,
  current: 0,
  statusLabel: '已处理',
  message: '',
  error: '',
  busy: false,
  canCancel: false,
  zIndex: 150,
  maxWidth: '32rem',
})

defineEmits<{
  close: []
  cancel: []
}>()

const progressValue = computed(() => {
  if (!props.total) return props.busy ? 12 : 100
  return Math.min(100, Math.max(0, Math.round((props.current / props.total) * 100)))
})
</script>

<style scoped>
.operation-progress-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  background: hsl(var(--card));
  padding: 10px 12px;
}

.operation-progress-card span {
  color: hsl(var(--muted-foreground));
  font-size: 11px;
}

.operation-progress-card strong {
  color: hsl(var(--foreground));
  font-size: 18px;
  font-weight: 700;
}

.operation-progress-details {
  border-top: 1px solid hsl(var(--border));
  padding-top: 12px;
}
</style>
