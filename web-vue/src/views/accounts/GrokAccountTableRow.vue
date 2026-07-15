<template>
  <tr
    class="border-t border-border transition-colors"
    :class="grokAccountRowClass(item)"
    v-memo="[signature, selected, runtimeAvailable, busy, syncing, refreshing, testing, chatting, toggling, deleting, oauthAction]"
  >
    <td class="py-4 pr-4 align-middle">
      <Checkbox
        :model-value="selected"
        @update:model-value="emit('toggle-select', item.id, $event)"
      />
    </td>
    <td class="py-4 pr-5 align-middle">
      <p class="max-w-[18rem] truncate text-sm font-medium text-foreground">{{ item.email || '-' }}</p>
      <p class="mt-1 max-w-[18rem] truncate font-mono text-xs text-muted-foreground" :title="item.id">
        {{ grokAccountTokenPreview(item) }}
      </p>
    </td>
    <td class="py-4 pr-5 align-middle">
      <div class="flex flex-wrap items-center gap-1.5">
        <p class="text-xs font-medium text-foreground">{{ grokAccountPoolText(item) }}</p>
        <StatusPill
          v-if="item.oauth"
          :label="grokOAuthStatusText(item)"
          :tone-class="`${grokOAuthStatusClass(item)} border-border`"
          :title="item.oauth.models.length ? `OAuth 模型：${item.oauth.models.join('、')}` : '已关联 OAuth 凭据'"
        />
      </div>
      <p class="mt-1 text-xs text-muted-foreground">{{ grokSyncStateText(item) }}</p>
    </td>
    <td class="py-4 pr-5 align-middle">
      <StatusPill
        :label="grokAccountStatusText(item)"
        :tone-class="`${grokAccountStatusClass(item)} border-border`"
      />
    </td>
    <td class="py-4 pr-5 align-middle">
      <StatusPill
        :label="grokRuntimeStatusText(item)"
        :tone-class="`${grokRuntimeStatusClass(item)} border-border`"
      />
    </td>
    <td class="max-w-[18rem] py-4 pr-5 align-middle font-mono text-xs text-muted-foreground">
      <span class="whitespace-normal leading-5">{{ grokQuotaText(item) }}</span>
    </td>
    <td class="py-4 pr-5 align-middle">
      <p class="font-mono text-sm tabular-nums">
        <span :class="item.sync_state === 'synced' ? 'text-emerald-600' : 'text-muted-foreground'">
          {{ item.sync_state === 'synced' ? (item.use_count || 0) : '-' }}
        </span>
        <span class="mx-1 text-muted-foreground/60">/</span>
        <span :class="item.sync_state === 'synced' ? 'text-rose-600' : 'text-muted-foreground'">
          {{ item.sync_state === 'synced' ? (item.fail_count || 0) : '-' }}
        </span>
      </p>
      <p class="mt-1 text-xs text-muted-foreground">成功率 {{ grokSuccessRate(item) }}</p>
    </td>
    <td class="py-4 pr-5 align-middle text-xs text-muted-foreground">
      {{ formatGrokAccountDate(item.last_used_at) }}
    </td>
    <td class="py-4 text-right align-middle">
      <GrokAccountActionButtons
        :item="item"
        :runtime-available="runtimeAvailable"
        :busy="busy"
        :syncing="syncing"
        :refreshing="refreshing"
        :testing="testing"
        :chatting="chatting"
        :toggling="toggling"
        :deleting="deleting"
        :oauth-account="item.oauth"
        :oauth-action="oauthAction"
        align="end"
        @credentials="emit('credentials', item)"
        @sync="emit('sync', item)"
        @refresh="emit('refresh', item)"
        @test="emit('test', item)"
        @chat="emit('chat', item)"
        @toggle-disabled="emit('toggle-disabled', item)"
        @remove="emit('remove', item)"
        @oauth-sync="emit('oauth-sync', item)"
        @oauth-refresh="emit('oauth-refresh', item)"
        @oauth-toggle="emit('oauth-toggle', item)"
        @oauth-remove="emit('oauth-remove', item)"
      />
    </td>
  </tr>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Checkbox, StatusPill } from 'nanocat-ui'

import GrokAccountActionButtons from '@/components/ai/GrokAccountActionButtons.vue'
import type { GrokAccount } from '@/api/grokAccounts'
import {
  formatGrokAccountDate,
  grokAccountPoolText,
  grokAccountRowClass,
  grokAccountRowSignature,
  grokAccountStatusClass,
  grokAccountStatusText,
  grokAccountTokenPreview,
  grokQuotaText,
  grokOAuthStatusClass,
  grokOAuthStatusText,
  grokRuntimeStatusClass,
  grokRuntimeStatusText,
  grokSuccessRate,
  grokSyncStateText,
} from './grokAccountView'

const props = withDefaults(defineProps<{
  item: GrokAccount
  selected?: boolean
  runtimeAvailable?: boolean
  busy?: boolean
  syncing?: boolean
  refreshing?: boolean
  testing?: boolean
  chatting?: boolean
  toggling?: boolean
  deleting?: boolean
  oauthAction?: string
}>(), {
  selected: false,
  runtimeAvailable: false,
  busy: false,
  syncing: false,
  refreshing: false,
  testing: false,
  chatting: false,
  toggling: false,
  deleting: false,
  oauthAction: '',
})

const signature = computed(() => grokAccountRowSignature(props.item))

const emit = defineEmits<{
  (e: 'toggle-select', id: string, checked: unknown): void
  (e: 'credentials', item: GrokAccount): void
  (e: 'sync', item: GrokAccount): void
  (e: 'refresh', item: GrokAccount): void
  (e: 'test', item: GrokAccount): void
  (e: 'chat', item: GrokAccount): void
  (e: 'toggle-disabled', item: GrokAccount): void
  (e: 'remove', item: GrokAccount): void
  (e: 'oauth-sync', item: GrokAccount): void
  (e: 'oauth-refresh', item: GrokAccount): void
  (e: 'oauth-toggle', item: GrokAccount): void
  (e: 'oauth-remove', item: GrokAccount): void
}>()
</script>
