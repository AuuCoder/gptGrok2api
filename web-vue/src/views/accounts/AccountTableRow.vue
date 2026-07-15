<template>
  <tr
    class="border-t border-border transition-colors"
    :class="[rowClass(item), selected ? 'bg-primary/5' : '']"
    v-memo="[signature, detailSignature, selected, refreshing, resetting]"
  >
    <td class="py-4 pr-4 align-middle">
      <Checkbox
        :model-value="selected"
        :disabled="item.is_demo"
        @update:model-value="emit('toggle-select', item.id, $event)"
      />
    </td>
    <td class="py-4 pr-5 align-middle">
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
    </td>
    <td class="py-4 pr-5 align-middle">
      <div class="space-y-1 text-xs">
        <p class="font-medium text-foreground">{{ accountSourceText(item) }}</p>
      </div>
    </td>
    <td class="py-4 pr-5 align-middle">
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
    </td>
    <td class="py-4 pr-5 align-middle">
      <p class="max-w-[16rem] truncate text-sm font-medium text-foreground">{{ accountPrimaryText(item) }}</p>
      <p class="mt-1 max-w-[16rem] truncate font-mono text-xs text-muted-foreground">{{ accountSecondaryText(item) }}</p>
    </td>
    <td class="py-4 pr-5 align-middle text-xs text-muted-foreground">
      {{ accountCreatedText(item) }}
    </td>
    <td class="py-4 pr-5 align-middle">
      <QuotaBadge :account="item" />
    </td>
    <td class="py-4 pr-5 align-middle text-xs text-muted-foreground">
      {{ accountRestoreText(item) }}
    </td>
    <td class="py-4 pr-5 align-middle">
      <div class="font-mono text-sm tabular-nums">
        <span class="text-emerald-600">{{ item.success_count || 0 }}</span>
        <span class="mx-1 text-muted-foreground/60">/</span>
        <span class="text-rose-600">{{ item.failure_count || 0 }}</span>
      </div>
    </td>
    <td class="py-4 text-right align-middle">
      <AccountActionButtons
        :item="item"
        :refreshing="refreshing"
        :resetting="resetting"
        align="end"
        @edit="emit('edit', item)"
        @toggle-enabled="emit('toggle-enabled', item)"
        @refresh-token="emit('refresh-token', item.id)"
        @reset-state="emit('reset-state', item.id)"
        @copy-final-checkout-link="emit('copy-final-checkout-link', item)"
        @open-final-checkout-link="emit('open-final-checkout-link', item)"
        @remove="emit('remove', item.id)"
      />
    </td>
  </tr>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Checkbox, StatusDetailPill, StatusPill } from 'nanocat-ui'

import AccountActionButtons from '@/components/ai/AccountActionButtons.vue'
import QuotaBadge from '@/components/ai/QuotaBadge.vue'
import type { Account } from '@/api/accounts'
import {
  accountCreatedText,
  accountPrimaryText,
  accountRestoreText,
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
