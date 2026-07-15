<template>
  <ModalShell :open="open" max-width="34rem" :z-index="150">
    <ModalHeader title="Grok 登录凭据" compact @close="close" />
    <ModalBody class="space-y-4">
      <PageLoadingState
        v-if="loading"
        title="正在读取登录凭据"
        description=""
      />
      <SurfaceBox v-else-if="error" tone="danger" density="compact">
        {{ error }}
      </SurfaceBox>
      <template v-else-if="credentials">
        <label class="block text-xs">
          <span class="ui-field-label">登录邮箱</span>
          <div class="flex gap-2">
            <Input :model-value="credentials.email" readonly block />
            <Button size="sm" variant="outline" root-class="shrink-0" @click="copy(credentials.email, '邮箱')">复制</Button>
          </div>
        </label>
        <label class="block text-xs">
          <span class="ui-field-label">登录密码</span>
          <div class="flex gap-2">
            <Input :model-value="credentials.password" readonly block />
            <Button size="sm" variant="outline" root-class="shrink-0" @click="copy(credentials.password, '密码')">复制</Button>
          </div>
        </label>
      </template>
    </ModalBody>
    <ModalFooter>
      <Button size="sm" variant="outline" @click="close">关闭</Button>
    </ModalFooter>
  </ModalShell>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { Button, Input } from 'nanocat-ui'

import type { GrokAccount, GrokAccountLoginCredentials } from '@/api/grokAccounts'
import { grokAccountsApi } from '@/api/grokAccounts'
import ModalBody from '@/components/ai/ModalBody.vue'
import ModalFooter from '@/components/ai/ModalFooter.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import PageLoadingState from '@/components/ai/PageLoadingState.vue'
import SurfaceBox from '@/components/ai/SurfaceBox.vue'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'

const props = withDefaults(defineProps<{
  open: boolean
  account: GrokAccount | null
}>(), {
  open: false,
  account: null,
})

const emit = defineEmits<{ (e: 'close'): void }>()

const toast = useToast()
const loading = ref(false)
const error = ref('')
const credentials = ref<GrokAccountLoginCredentials | null>(null)
let requestVersion = 0

function clear() {
  requestVersion += 1
  loading.value = false
  error.value = ''
  credentials.value = null
}

function close() {
  clear()
  emit('close')
}

async function loadCredentials(accountId: string) {
  const version = ++requestVersion
  loading.value = true
  error.value = ''
  credentials.value = null
  try {
    const result = await grokAccountsApi.loginCredentials(accountId)
    if (version !== requestVersion) return
    credentials.value = result
  } catch (cause) {
    if (version !== requestVersion) return
    error.value = errorMessage(cause)
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

async function copy(value: string, label: string) {
  try {
    await navigator.clipboard.writeText(value)
    toast.success(`${label}已复制`)
  } catch {
    toast.error(`${label}复制失败`)
  }
}

watch(
  () => [props.open, props.account?.id] as const,
  ([open, accountId]) => {
    if (!open || !accountId) {
      clear()
      return
    }
    void loadCredentials(accountId)
  },
  { immediate: true },
)
</script>
