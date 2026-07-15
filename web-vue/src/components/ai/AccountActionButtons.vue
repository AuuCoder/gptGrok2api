<template>
  <div class="flex items-center gap-2" :class="alignClass">
    <Button
      size="xs"
      variant="outline"
      root-class="w-14 justify-center"
      :disabled="item.is_demo"
      @click="emit('edit')"
    >
      编辑
    </Button>
    <FloatingActionMenu
      label="更多"
      :items="menuItems"
      :disabled="item.is_demo"
      align="right"
      size="sm"
      trigger-class="h-7 justify-center px-2 text-[11px]"
      :trigger-width="64"
      @select="handleSelect"
    />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Button } from 'nanocat-ui'
import type { ActionMenuItem } from 'nanocat-ui'
import type { Account } from '@/api/accounts'
import FloatingActionMenu from './FloatingActionMenu.vue'
import { actionMenuGroups } from './menuItems'

const props = withDefaults(defineProps<{
  item: Account
  refreshing?: boolean
  resetting?: boolean
  align?: 'start' | 'end'
}>(), {
  refreshing: false,
  resetting: false,
  align: 'start',
})

const emit = defineEmits<{
  (e: 'edit'): void
  (e: 'toggle-enabled'): void
  (e: 'refresh-token'): void
  (e: 'reset-state'): void
  (e: 'copy-final-checkout-link'): void
  (e: 'open-final-checkout-link'): void
  (e: 'remove'): void
}>()

const alignClass = computed(() => (
  props.align === 'end' ? 'justify-end' : 'justify-start'
))

function finalCheckoutLinkUrl(item: Account): string {
  const finalUrl = String(item.checkout_final_url || '').trim()
  if (finalUrl) return finalUrl
  return String(item.checkout_final_kind || '').trim()
    ? String(item.checkout_url || '').trim()
    : ''
}

const hasFinalCheckoutLink = computed(() => Boolean(finalCheckoutLinkUrl(props.item)))

const menuItems = computed<ActionMenuItem[]>(() => actionMenuGroups(
  [
    {
      key: 'copy-final-checkout-link',
      label: '复制最终支付链接',
      disabled: !hasFinalCheckoutLink.value,
    },
    {
      key: 'open-final-checkout-link',
      label: '打开最终支付链接',
      disabled: !hasFinalCheckoutLink.value,
    },
  ],
  [
    {
      key: 'refresh-token',
      label: props.refreshing ? '刷新中...' : '刷新账号信息和额度',
      disabled: props.refreshing,
    },
    {
      key: 'reset-state',
      label: props.resetting ? '重置中...' : '重置状态',
      disabled: props.resetting,
    },
  ],
  [
    {
      key: 'toggle-enabled',
      label: props.item.enabled ? '禁用账号' : '启用账号',
    },
  ],
  [
    {
      key: 'remove',
      label: '删除账号',
      danger: true,
    },
  ],
))

function handleSelect(key: string) {
  if (key === 'copy-final-checkout-link') emit('copy-final-checkout-link')
  if (key === 'open-final-checkout-link') emit('open-final-checkout-link')
  if (key === 'toggle-enabled') emit('toggle-enabled')
  if (key === 'refresh-token') emit('refresh-token')
  if (key === 'reset-state') emit('reset-state')
  if (key === 'remove') emit('remove')
}
</script>
