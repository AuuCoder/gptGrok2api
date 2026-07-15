<template>
  <article
    class="ui-card flex h-full flex-col gap-4 transition-all hover:border-primary/30"
    :class="[grokAccountRowClass(item), selected ? 'ring-2 ring-primary/30' : '']"
    v-memo="[signature, selected, runtimeAvailable, busy, syncing, refreshing, testing, chatting, toggling, deleting, oauthAction]"
  >
    <div class="flex items-start justify-between gap-3">
      <div class="flex min-w-0 items-start gap-3">
        <Checkbox
          :model-value="selected"
          @update:model-value="emit('toggle-select', item.id, $event)"
        />
        <div class="min-w-0">
          <h3 class="truncate text-sm font-medium text-foreground">{{ item.email || '-' }}</h3>
          <p class="mt-1 truncate font-mono text-xs text-muted-foreground" :title="item.id">
            {{ grokAccountTokenPreview(item) }}
          </p>
        </div>
      </div>
      <StatusPill
        :label="grokAccountStatusText(item)"
        :tone-class="`${grokAccountStatusClass(item)} border-border`"
      />
    </div>

    <div class="flex flex-wrap gap-1.5">
      <StatusPill
        :label="grokAccountPoolText(item)"
        tone-class="border-cyan-500/40 bg-cyan-500/10 text-cyan-600"
      />
      <StatusPill
        :label="grokRuntimeStatusText(item)"
        :tone-class="`${grokRuntimeStatusClass(item)} border-border`"
      />
      <StatusPill :label="grokSyncStateText(item)" tone-class="border-muted bg-muted/20 text-muted-foreground" />
      <StatusPill
        v-if="item.oauth"
        :label="grokOAuthStatusText(item)"
        :tone-class="`${grokOAuthStatusClass(item)} border-border`"
        :title="item.oauth.models.length ? `OAuth 模型：${item.oauth.models.join('、')}` : '已关联 OAuth 凭据'"
      />
    </div>

    <KeyValueList :items="grokAccountDetailItems(item)" :columns="2" />

    <GrokAccountActionButtons
      class="mt-auto"
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
  </article>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Checkbox, KeyValueList, StatusPill } from 'nanocat-ui'

import GrokAccountActionButtons from '@/components/ai/GrokAccountActionButtons.vue'
import type { GrokAccount } from '@/api/grokAccounts'
import {
  grokAccountDetailItems,
  grokAccountPoolText,
  grokAccountRowClass,
  grokAccountRowSignature,
  grokAccountStatusClass,
  grokAccountStatusText,
  grokAccountTokenPreview,
  grokOAuthStatusClass,
  grokOAuthStatusText,
  grokRuntimeStatusClass,
  grokRuntimeStatusText,
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
