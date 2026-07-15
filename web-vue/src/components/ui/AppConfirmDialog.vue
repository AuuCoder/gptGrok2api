<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-[300] overflow-y-auto bg-black/40 px-3 py-4"
      @click.self="$emit('cancel')"
    >
      <div class="flex min-h-full items-center justify-center">
        <div
          class="w-full max-w-sm rounded-2xl border border-border bg-card shadow-[var(--shadow-floating)]"
          role="dialog"
          aria-modal="true"
        >
          <div class="px-5 pb-0 pt-5">
            <h4 class="ui-dialog-title">{{ title || '确认操作' }}</h4>
          </div>
          <div class="ui-dialog-body whitespace-pre-line px-5 pb-5 pt-3">
            {{ message }}
          </div>
          <div class="flex items-center justify-end gap-2 px-5 pb-5 pt-0">
            <Button
              size="xs"
              variant="outline"
              root-class="min-w-14 justify-center text-muted-foreground"
              @click="$emit('cancel')"
            >
              {{ cancelText || '取消' }}
            </Button>
            <Button
              size="xs"
              variant="primary"
              root-class="min-w-14 justify-center"
              @click="$emit('confirm')"
            >
              {{ confirmText || '确定' }}
            </Button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { Button } from 'nanocat-ui'

defineProps<{
  open: boolean
  title?: string
  message: string
  confirmText?: string
  cancelText?: string
}>()

defineEmits<{
  confirm: []
  cancel: []
}>()
</script>
