<template>
  <ModalShell :open="open" max-width="44rem" :z-index="150">
    <ModalHeader
      title="Grok 账号对话测试"
      :subtitle="account?.email || ''"
      compact
      :close-disabled="running"
      @close="close"
    />
    <ModalBody density="compact" class="space-y-4">
      <SurfaceBox tone="muted" density="compact" class="space-y-3">
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div class="min-w-0 text-xs">
            <span class="ui-field-label">测试账号</span>
            <p class="truncate font-mono text-sm text-foreground" :title="account?.email || ''">
              {{ account?.email || '-' }}
            </p>
          </div>
          <label class="text-xs">
            <span class="ui-field-label">模型</span>
            <Input :model-value="model" readonly block />
          </label>
          <div class="min-w-0 text-xs">
            <span class="ui-field-label">Console 额度</span>
            <p :class="consoleQuotaExhausted ? 'font-mono text-sm text-rose-600' : 'font-mono text-sm text-foreground'">
              {{ consoleQuotaText }}
            </p>
            <p v-if="consoleQuotaRecoveryText" class="mt-1 text-xs text-rose-600">
              预计恢复 {{ consoleQuotaRecoveryText }}
            </p>
          </div>
        </div>
        <p class="font-mono text-xs text-muted-foreground" :title="account?.id || ''">
          {{ account?.id || '-' }}
        </p>
      </SurfaceBox>

      <label class="block text-xs">
        <span class="ui-field-label">测试提示词</span>
        <textarea
          v-model="prompt"
          rows="4"
          class="ui-textarea-sm resize-y"
          :disabled="running"
          placeholder="输入要发送给该账号的测试提示词"
          @keydown.meta.enter.prevent="runChatTest"
          @keydown.ctrl.enter.prevent="runChatTest"
        ></textarea>
      </label>

      <div v-if="elapsedMs !== null" class="flex items-center justify-between text-xs text-muted-foreground">
        <span>本次对话耗时</span>
        <span class="font-mono">{{ formatElapsed(elapsedMs) }}</span>
      </div>

      <SurfaceBox v-if="consoleQuotaExhausted" tone="danger" density="compact">
        当前账号的 Console 对话额度已耗尽。请关闭后刷新状态和额度，或选择 Console 额度大于 0 的账号。
      </SurfaceBox>
      <SurfaceBox v-else-if="requestError" tone="danger" density="compact">
        {{ requestError }}
      </SurfaceBox>
      <SurfaceBox v-else-if="hasResult" tone="muted" density="compact" class="space-y-2">
        <p class="ui-field-label">回答</p>
        <pre class="max-h-80 overflow-auto whitespace-pre-wrap break-words font-sans text-sm leading-6 text-foreground">{{ resultContent }}</pre>
      </SurfaceBox>
    </ModalBody>
    <ModalFooter>
      <Button size="sm" variant="outline" :disabled="running" @click="close">关闭</Button>
      <Button size="sm" variant="primary" :disabled="running || !account || !prompt.trim() || consoleQuotaExhausted" @click="runChatTest">
        {{ running ? '对话中...' : '发送测试消息' }}
      </Button>
    </ModalFooter>
  </ModalShell>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Button, Input } from 'nanocat-ui'

import { grokAccountsApi, type GrokAccount } from '@/api/grokAccounts'
import ModalBody from '@/components/ai/ModalBody.vue'
import ModalFooter from '@/components/ai/ModalFooter.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import SurfaceBox from '@/components/ai/SurfaceBox.vue'
import { errorMessage } from '@/lib/errorMessage'
import { formatGrokAccountDate } from './grokAccountView'

const DEFAULT_MODEL = 'grok-4.3-console'
const DEFAULT_PROMPT = '你好，请只回复 OK。'

const props = withDefaults(defineProps<{
  open: boolean
  account: GrokAccount | null
}>(), {
  open: false,
  account: null,
})

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'running', accountId: string, running: boolean): void
}>()

const prompt = ref(DEFAULT_PROMPT)
const running = ref(false)
const resultContent = ref('')
const requestError = ref('')
const elapsedMs = ref<number | null>(null)
const model = ref(DEFAULT_MODEL)
const hasResult = computed(() => Boolean(resultContent.value))
const consoleQuota = computed(() => props.account?.quota?.console || null)
function quotaNumber(value: unknown): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : 0
}

const consoleQuotaTotal = computed(() => quotaNumber(consoleQuota.value?.total))
const consoleQuotaRemaining = computed(() => quotaNumber(consoleQuota.value?.remaining))
const consoleQuotaSource = computed(() => quotaNumber(consoleQuota.value?.source))
const consoleQuotaResetAt = computed(() => quotaNumber(consoleQuota.value?.reset_at))
const consoleQuotaKnown = computed(() => consoleQuotaTotal.value > 0)
const consoleQuotaExhausted = computed(() => (
  consoleQuotaKnown.value
  && consoleQuotaRemaining.value <= 0
  && consoleQuotaSource.value === 2
  && (consoleQuotaResetAt.value <= 0 || consoleQuotaResetAt.value > Date.now())
))
const consoleQuotaText = computed(() => (
  consoleQuotaKnown.value
    ? `${consoleQuotaRemaining.value} / ${consoleQuotaTotal.value}`
    : '未同步'
))
const consoleQuotaRecoveryText = computed(() => (
  consoleQuotaExhausted.value && consoleQuotaResetAt.value > Date.now()
    ? formatGrokAccountDate(consoleQuotaResetAt.value)
    : ''
))
let requestVersion = 0

function resetResult() {
  resultContent.value = ''
  requestError.value = ''
  elapsedMs.value = null
  model.value = DEFAULT_MODEL
}

function resetForAccount() {
  prompt.value = DEFAULT_PROMPT
  resetResult()
}

function close() {
  if (running.value) return
  resetForAccount()
  emit('close')
}

function formatElapsed(value: number) {
  return `${(value / 1000).toFixed(1)}s`
}

function resolveElapsedMs(value: unknown, startedAt: number) {
  const elapsed = Number(value)
  return Number.isFinite(elapsed) && elapsed >= 0
    ? Math.round(elapsed)
    : Math.round(performance.now() - startedAt)
}

async function runChatTest() {
  const accountId = String(props.account?.id || '').trim()
  const input = prompt.value.trim()
  if (!accountId || running.value) return
  if (!input) {
    requestError.value = '请输入测试提示词'
    return
  }
  if (consoleQuotaExhausted.value) {
    requestError.value = '该账号的 Console 对话额度已耗尽，请选择额度可用的账号。'
    return
  }

  const version = ++requestVersion
  const startedAt = performance.now()
  resetResult()
  running.value = true
  emit('running', accountId, true)
  try {
    const result = await grokAccountsApi.chatTest(accountId, {
      prompt: input,
      model: DEFAULT_MODEL,
    })
    if (version !== requestVersion) return
    model.value = String(result.model || DEFAULT_MODEL)
    resultContent.value = String(result.content ?? '') || '（上游未返回文本内容）'
    elapsedMs.value = resolveElapsedMs(result.elapsed_ms, startedAt)
  } catch (error) {
    if (version !== requestVersion) return
    requestError.value = errorMessage(error, '对话测试失败')
    elapsedMs.value = Math.round(performance.now() - startedAt)
  } finally {
    if (version === requestVersion) {
      running.value = false
      emit('running', accountId, false)
    }
  }
}

watch(
  () => [props.open, props.account?.id] as const,
  ([open, accountId], previous) => {
    if (!open || !accountId || running.value) return
    if (!previous || previous[0] !== open || previous[1] !== accountId) resetForAccount()
  },
  { immediate: true },
)
</script>
