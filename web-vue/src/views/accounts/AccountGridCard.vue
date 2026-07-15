<template>
  <article
    class="ui-card flex h-full flex-col gap-4 transition-all"
    :class="[rowClass(item), selected ? 'ring-2 ring-primary/30' : 'hover:border-primary/30']"
    v-memo="[signature, detailSignature, selected, refreshing, resetting]"
  >
    <div class="flex items-start justify-between gap-3">
      <div class="flex min-w-0 items-start gap-3">
        <Checkbox
          :model-value="selected"
          :disabled="item.is_demo"
          @update:model-value="emit('toggle-select', item.id, $event)"
        />
        <div class="min-w-0">
          <h3 class="truncate text-sm font-medium text-foreground">{{ accountPrimaryText(item) }}</h3>
          <p class="mt-1 truncate font-mono text-xs text-muted-foreground">{{ accountSecondaryText(item) }}</p>
        </div>
      </div>
      <StatusDetailPill
        :label="statusText(item)"
        :tone-class="`${statusClass(item)} border-border`"
        title="状态详情"
        detail-label="状态说明"
        raw-error-label="原始报错"
        :card-class="statusDetailCardClass"
        :detail="statusDetailText(item)"
        :raw-error="statusRawError(item)"
      />
    </div>

    <div class="flex flex-wrap items-center gap-2">
      <StatusPill
        :label="accountSourceText(item)"
        tone-class="border-cyan-500/40 bg-cyan-500/10 text-cyan-600"
      />
      <button
        type="button"
        class="text-left"
        title="点击复制完整 Token"
        @click="emit('copy-token', item)"
      >
        <StatusPill
          :label="accountTokenPreview(item)"
          tone-class="border-muted bg-muted/20 text-muted-foreground"
          title="Access Token"
          detail="点击复制完整 Token"
          card-class="w-48"
        />
      </button>
    </div>

    <KeyValueList
      :items="accountDetailItems(item)"
      :columns="2"
    />

    <AccountActionButtons
      class="mt-auto"
      :item="item"
      :refreshing="refreshing"
      :resetting="resetting"
      @edit="emit('edit', item)"
      @toggle-enabled="emit('toggle-enabled', item)"
      @refresh-token="emit('refresh-token', item.id)"
      @reset-state="emit('reset-state', item.id)"
      @copy-final-checkout-link="emit('copy-final-checkout-link', item)"
      @open-final-checkout-link="emit('open-final-checkout-link', item)"
      @remove="emit('remove', item.id)"
    />
  </article>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Checkbox, KeyValueList, StatusDetailPill, StatusPill } from 'nanocat-ui'

import AccountActionButtons from '@/components/ai/AccountActionButtons.vue'
import type { Account } from '@/api/accounts'
import {
  accountDetailItems,
  accountPrimaryText,
  accountRowSignature,
  accountSecondaryText,
  accountSourceText,
  accountTokenPreview,
  boundedSignatureText,
  rowClass,
  statusClass,
  statusRawError,
  statusText,
} from './viewUtils'

const props = withDefaults(defineProps<{
  item: Account
  selected: boolean
  refreshing?: boolean
  resetting?: boolean
  statusDetailCardClass?: string
  statusDetailText: (item: Account) => string
}>(), {
  refreshing: false,
  resetting: false,
  statusDetailCardClass: '',
})

const signature = computed(() => accountRowSignature(props.item))
const detailSignature = computed(() => boundedSignatureText(props.statusDetailText(props.item)))

const emit = defineEmits<{
  (e: 'toggle-select', id: string, checked: unknown): void
  (e: 'copy-token', item: Account): void
  (e: 'edit', item: Account): void
  (e: 'toggle-enabled', item: Account): void
  (e: 'refresh-token', id: string): void
  (e: 'reset-state', id: string): void
  (e: 'copy-final-checkout-link', item: Account): void
  (e: 'open-final-checkout-link', item: Account): void
  (e: 'remove', id: string): void
}>()
</script>
