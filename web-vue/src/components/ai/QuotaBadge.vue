<template>
  <MetaChip
    :tone="quotaTone"
    size="xs"
    strong
    chip-class="min-w-[2.75rem] font-mono tabular-nums"
  >
    {{ quotaText }}
  </MetaChip>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Account } from '@/api/accounts'
import MetaChip from './MetaChip.vue'

const props = defineProps<{
  account: Account
}>()

const quotaValue = computed(() => Number(props.account.quota || 0))

const quotaText = computed(() => {
  if (props.account.image_quota_unknown) return '未知'
  return String(Math.max(0, Math.trunc(quotaValue.value)))
})

const quotaTone = computed(() => {
  if (props.account.image_quota_unknown) {
    return 'muted'
  }
  if (quotaValue.value <= 0) {
    return 'danger'
  }
  if (quotaValue.value <= 3) {
    return 'warning'
  }
  return 'success'
})
</script>
