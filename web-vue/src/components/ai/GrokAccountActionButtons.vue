<template>
  <div class="flex items-center gap-2" :class="alignClass">
    <Button
      size="xs"
      variant="outline"
      root-class="w-14 justify-center"
      :disabled="busy || !item.has_sso || !runtimeAvailable || syncing || chatting"
      @click="emit('sync')"
    >
      {{ syncing ? '加入中' : (item.sync_state === 'synced' ? '重新加入' : '加入') }}
    </Button>
    <Button
      size="xs"
      variant="outline"
      root-class="w-14 justify-center"
      :disabled="busy || !item.has_sso || testing || chatting"
      @click="emit('test')"
    >
      {{ testing ? '测试中' : '测试' }}
    </Button>
    <Button
      size="xs"
      variant="outline"
      root-class="w-14 justify-center"
      :disabled="busy || !item.has_sso || chatting"
      @click="emit('chat')"
    >
      {{ chatting ? '对话中' : '对话' }}
    </Button>
    <FloatingActionMenu
      label="更多"
      :items="menuItems"
      :disabled="busy || syncing || refreshing || testing || chatting || toggling || deleting"
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

import type { GrokAccount } from '@/api/grokAccounts'
import type { GrokOAuthAccount } from '@/api/grokOAuthAccounts'
import FloatingActionMenu from './FloatingActionMenu.vue'
import { actionMenuGroups } from './menuItems'

const props = withDefaults(defineProps<{
  item: GrokAccount
  runtimeAvailable?: boolean
  busy?: boolean
  syncing?: boolean
  refreshing?: boolean
  testing?: boolean
  chatting?: boolean
  toggling?: boolean
  deleting?: boolean
  oauthAccount?: GrokOAuthAccount | null
  oauthAction?: string
  align?: 'start' | 'end'
}>(), {
  runtimeAvailable: false,
  busy: false,
  syncing: false,
  refreshing: false,
  testing: false,
  chatting: false,
  toggling: false,
  deleting: false,
  oauthAccount: null,
  oauthAction: '',
  align: 'start',
})

const emit = defineEmits<{
  (e: 'credentials'): void
  (e: 'sync'): void
  (e: 'refresh'): void
  (e: 'test'): void
  (e: 'chat'): void
  (e: 'toggle-disabled'): void
  (e: 'remove'): void
  (e: 'oauth-sync'): void
  (e: 'oauth-refresh'): void
  (e: 'oauth-toggle'): void
  (e: 'oauth-remove'): void
}>()

const alignClass = computed(() => props.align === 'end' ? 'justify-end' : 'justify-start')
const isRuntimeDisabled = computed(() => String(props.item.runtime_status || '').toLowerCase() === 'disabled')
const canManageRuntime = computed(() => props.runtimeAvailable && props.item.sync_state === 'synced')
const oauthActionBusy = computed(() => Boolean(props.oauthAction))

const menuItems = computed<ActionMenuItem[]>(() => actionMenuGroups(
  [
    {
      key: 'credentials',
      label: '登录凭据',
      disabled: !props.item.has_password,
    },
    {
      key: 'refresh',
      label: props.refreshing ? '刷新中...' : '刷新状态和额度',
      disabled: !canManageRuntime.value || props.refreshing,
    },
    {
      key: 'toggle-disabled',
      label: props.toggling ? '处理中...' : (isRuntimeDisabled.value ? '恢复账号' : '禁用账号'),
      disabled: !canManageRuntime.value || props.toggling,
    },
  ],
  props.oauthAccount ? [
    {
      key: 'oauth-sync',
      label: props.oauthAction === 'sync' ? 'OAuth 探测中...' : 'OAuth 探测模型',
      disabled: oauthActionBusy.value,
    },
    {
      key: 'oauth-refresh',
      label: props.oauthAction === 'refresh' ? 'OAuth 刷新中...' : 'OAuth 刷新 Token',
      disabled: oauthActionBusy.value,
    },
    {
      key: 'oauth-toggle',
      label: props.oauthAction === 'toggle'
        ? 'OAuth 处理中...'
        : (props.oauthAccount.status === 'disabled' ? '启用 OAuth' : '禁用 OAuth'),
      disabled: oauthActionBusy.value,
    },
    {
      key: 'oauth-remove',
      label: props.oauthAction === 'remove' ? 'OAuth 移除中...' : '移除 OAuth',
      disabled: oauthActionBusy.value,
      danger: true,
    },
  ] : [],
  [
    {
      key: 'remove',
      label: props.deleting ? '删除中...' : '删除账号',
      disabled: props.deleting,
      danger: true,
    },
  ],
))

function handleSelect(key: string) {
  if (key === 'credentials') emit('credentials')
  if (key === 'refresh') emit('refresh')
  if (key === 'toggle-disabled') emit('toggle-disabled')
  if (key === 'remove') emit('remove')
  if (key === 'oauth-sync') emit('oauth-sync')
  if (key === 'oauth-refresh') emit('oauth-refresh')
  if (key === 'oauth-toggle') emit('oauth-toggle')
  if (key === 'oauth-remove') emit('oauth-remove')
}
</script>
