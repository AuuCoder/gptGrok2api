<template>
  <ModalShell :open="props.open" max-width="48rem" :z-index="150" @close="emit('close')">
    <ModalHeader
      title="OAuth 接入"
      :subtitle="`${accounts.length} 个 OAuth 账号 · ${availableModels.length ? availableModels.join('、') : '暂无已探测模型'}`"
      compact
      @close="emit('close')"
    />
    <ModalBody density="compact" class="space-y-4">
      <div class="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" :disabled="busy || loading" @click="loadAccounts">
          {{ loading ? '刷新中...' : '刷新' }}
        </Button>
        <Button size="sm" variant="outline" :disabled="busy || !availableModels.length" @click="openModelTest">
          测试模型
        </Button>
        <Button size="sm" variant="outline" :disabled="busy" @click="showImport = !showImport">
          导入凭据
        </Button>
        <Button size="sm" variant="outline" :disabled="busy || protocolRunning" @click="startDeviceAuthorization">
          手动设备码
        </Button>
        <Button size="sm" variant="primary" :disabled="busy || protocolRunning" @click="startProtocolAuthorization">
          {{ protocolRunning ? '协议连接中...' : '一键协议连接' }}
        </Button>
      </div>

      <SurfaceBox v-if="protocolJob" tone="muted" density="compact" class="space-y-2">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <span class="text-sm font-medium text-foreground">协议授权 · {{ protocolStageLabel }}</span>
          <StateBadge :tone="protocolJobTone" size="xs">{{ protocolStatusLabel }}</StateBadge>
        </div>
        <p class="text-xs text-muted-foreground">{{ protocolJob.message }}</p>
        <p v-if="protocolJob.error" class="break-words text-xs text-rose-600">{{ protocolJob.error }}</p>
      </SurfaceBox>

      <SurfaceBox v-if="device" tone="muted" density="compact" class="space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <span class="font-mono text-sm text-foreground">{{ device.user_code }}</span>
          <div class="flex flex-wrap gap-2">
            <Button size="xs" variant="outline" @click="openDeviceAuthorization">打开授权页</Button>
            <Button size="xs" variant="primary" :disabled="busy" @click="pollDeviceAuthorization">检查授权</Button>
          </div>
        </div>
        <p class="truncate font-mono text-xs text-muted-foreground" :title="device.verification_uri_complete">
          {{ device.verification_uri_complete }}
        </p>
      </SurfaceBox>

      <SurfaceBox v-if="showImport" tone="muted" density="compact" class="space-y-3">
        <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
          <label class="text-xs md:col-span-2">
            <span class="ui-field-label">CPA OAuth JSON</span>
            <textarea v-model.trim="credentialText" rows="5" class="ui-textarea-sm font-mono" placeholder="粘贴 xAI OAuth CPA JSON"></textarea>
          </label>
          <label class="text-xs">
            <span class="ui-field-label">Access Token</span>
            <textarea v-model.trim="accessToken" rows="3" class="ui-textarea-sm font-mono" placeholder="可选：直接粘贴"></textarea>
          </label>
          <label class="text-xs">
            <span class="ui-field-label">Refresh Token</span>
            <textarea v-model.trim="refreshToken" rows="3" class="ui-textarea-sm font-mono" placeholder="可选：直接粘贴"></textarea>
          </label>
        </div>
        <div class="flex justify-end gap-2">
          <Button size="xs" variant="outline" :disabled="busy" @click="showImport = false">取消</Button>
          <Button size="xs" variant="primary" :disabled="busy || !hasImportValue" @click="importCredential">
            {{ busy ? '导入中...' : '导入' }}
          </Button>
        </div>
      </SurfaceBox>

      <SurfaceBox v-if="showModelTest" tone="muted" density="compact" class="space-y-3">
        <div class="grid grid-cols-1 gap-3 md:grid-cols-[minmax(12rem,0.35fr)_1fr]">
          <div class="text-xs">
            <span class="ui-field-label">模型</span>
            <GroupedSelectMenu
              v-model="testModel"
              :options="modelTestOptions"
              placeholder="选择模型"
              selected-indicator="none"
              aria-label="Grok 测试模型"
              block
            />
          </div>
          <label class="text-xs">
            <span class="ui-field-label">测试提示词</span>
            <textarea v-model.trim="testPrompt" rows="2" class="ui-textarea-sm" placeholder="输入测试提示词"></textarea>
          </label>
        </div>
        <div class="flex flex-wrap items-center justify-between gap-2">
          <p v-if="testElapsedMs !== null" class="text-xs text-muted-foreground">耗时 {{ (testElapsedMs / 1000).toFixed(1) }}s</p>
          <span v-else></span>
          <div class="flex gap-2">
            <Button size="xs" variant="outline" :disabled="busy" @click="closeModelTest">收起</Button>
            <Button size="xs" variant="primary" :disabled="busy || !testModel || !testPrompt" @click="runModelTest">
              {{ busy ? '测试中...' : '开始测试' }}
            </Button>
          </div>
        </div>
        <p v-if="testError" class="text-xs text-rose-600">{{ testError }}</p>
        <pre v-else-if="testResult" class="max-h-52 overflow-auto whitespace-pre-wrap break-words border-t border-border pt-3 text-sm text-foreground">{{ testResult }}</pre>
      </SurfaceBox>
    </ModalBody>
    <ModalFooter>
      <Button size="sm" variant="outline" :disabled="busy" @click="emit('close')">完成</Button>
    </ModalFooter>
  </ModalShell>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Button } from 'nanocat-ui'

import {
  grokOAuthAccountsApi,
  type GrokOAuthAccount,
  type GrokOAuthDeviceSession,
  type GrokOAuthProtocolJob,
} from '@/api/grokOAuthAccounts'
import { streamChatCompletion } from '@/api/chatStream'
import StateBadge from '@/components/ai/StateBadge.vue'
import SurfaceBox from '@/components/ai/SurfaceBox.vue'
import ModalBody from '@/components/ai/ModalBody.vue'
import ModalFooter from '@/components/ai/ModalFooter.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import GroupedSelectMenu from '@/components/ui/GroupedSelectMenu.vue'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'

const toast = useToast()
const props = withDefaults(defineProps<{ open?: boolean }>(), { open: false })
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'changed'): void
}>()
const accounts = ref<GrokOAuthAccount[]>([])
const availableModels = ref<string[]>([])
const loading = ref(false)
const busy = ref(false)
const showImport = ref(false)
const device = ref<GrokOAuthDeviceSession | null>(null)
const protocolJob = ref<GrokOAuthProtocolJob | null>(null)
const credentialText = ref('')
const accessToken = ref('')
const refreshToken = ref('')
const showModelTest = ref(false)
const testModel = ref('')
const testPrompt = ref('你好，请只回复 OK。')
const testResult = ref('')
const testError = ref('')
const testElapsedMs = ref<number | null>(null)
let protocolPollTimer: number | null = null

const hasImportValue = computed(() => Boolean(credentialText.value || (accessToken.value && refreshToken.value)))
const modelTestOptions = computed(() => availableModels.value.map((model) => ({ label: model, value: model })))
const protocolRunning = computed(() => ['pending', 'running'].includes(protocolJob.value?.status || ''))
const protocolJobTone = computed(() => {
  if (protocolJob.value?.status === 'authorized') return 'success'
  if (protocolJob.value?.status === 'failed') return 'danger'
  return 'warning'
})
const protocolStatusLabel = computed(() => ({
  pending: '等待',
  running: '运行中',
  authorized: '已完成',
  failed: '失败',
}[protocolJob.value?.status || 'pending'] || protocolJob.value?.status || '等待'))
const protocolStageLabel = computed(() => ({
  queued: '排队',
  bootstrap: '发现协议参数',
  device_code: '创建设备码',
  signin: '建立登录上下文',
  castle: '生成 Castle token',
  turnstile: '求解 Turnstile',
  session: '创建账号会话',
  consent: '读取授权主体',
  approve: '确认 Allow',
  token: '获取 OAuth token',
  models: '探测模型',
  completed: '完成',
  failed: '失败',
}[protocolJob.value?.stage || 'queued'] || protocolJob.value?.stage || '排队'))

async function loadAccounts() {
  loading.value = true
  try {
    const result = await grokOAuthAccountsApi.list()
    accounts.value = result.items || []
    availableModels.value = result.available_models || []
    if (!availableModels.value.includes(testModel.value)) testModel.value = availableModels.value[0] || ''
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'OAuth 账号加载失败')
  } finally {
    loading.value = false
  }
}

function openModelTest() {
  if (!testModel.value) testModel.value = availableModels.value[0] || ''
  showModelTest.value = true
}

function closeModelTest() {
  showModelTest.value = false
  testResult.value = ''
  testError.value = ''
  testElapsedMs.value = null
}

async function runModelTest() {
  const model = testModel.value.trim()
  const prompt = testPrompt.value.trim()
  if (!model || !prompt) return

  busy.value = true
  testResult.value = ''
  testError.value = ''
  testElapsedMs.value = null
  const startedAt = performance.now()
  try {
    const result = await streamChatCompletion({
      model,
      messages: [{ role: 'user', content: prompt }],
      onDelta: (delta) => { testResult.value += delta },
    })
    if (!testResult.value) testResult.value = result.content
    testElapsedMs.value = Math.round(performance.now() - startedAt)
    toast.success(`${model} 测试成功`)
    await loadAccounts()
  } catch (error) {
    testElapsedMs.value = Math.round(performance.now() - startedAt)
    testError.value = errorMessage(error, '模型测试失败')
  } finally {
    busy.value = false
  }
}

async function startDeviceAuthorization() {
  busy.value = true
  try {
    device.value = await grokOAuthAccountsApi.startDevice()
    window.open(device.value.verification_uri_complete, '_blank', 'noopener,noreferrer')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '无法创建设备授权')
  } finally {
    busy.value = false
  }
}

function scheduleProtocolPoll() {
  if (protocolPollTimer !== null) window.clearTimeout(protocolPollTimer)
  protocolPollTimer = window.setTimeout(pollProtocolAuthorization, 1500)
}

async function pollProtocolAuthorization() {
  const jobId = protocolJob.value?.id
  if (!jobId) return
  try {
    const result = await grokOAuthAccountsApi.getProtocolJob(jobId)
    protocolJob.value = result.job
    if (result.job.status === 'authorized') {
      toast.success(`协议授权成功${result.job.models.length ? `：${result.job.models.join('、')}` : ''}`)
      await loadAccounts()
      emit('changed')
      return
    }
    if (result.job.status === 'failed') {
      toast.error(result.job.error || '协议授权失败')
      return
    }
    scheduleProtocolPoll()
  } catch (error) {
    toast.error(errorMessage(error, '协议授权状态读取失败'))
  }
}

async function startProtocolAuthorization() {
  busy.value = true
  try {
    const result = await grokOAuthAccountsApi.startProtocol()
    protocolJob.value = result.job
    scheduleProtocolPoll()
  } catch (error) {
    toast.error(errorMessage(error, '无法启动协议授权'))
  } finally {
    busy.value = false
  }
}

function openDeviceAuthorization() {
  if (device.value?.verification_uri_complete) {
    window.open(device.value.verification_uri_complete, '_blank', 'noopener,noreferrer')
  }
}

async function pollDeviceAuthorization() {
  if (!device.value) return
  busy.value = true
  try {
    const result = await grokOAuthAccountsApi.pollDevice(device.value.id)
    if (result.status === 'authorized') {
      device.value = null
      toast.success('Grok Build OAuth 账号已连接')
      await loadAccounts()
      emit('changed')
      return
    }
    toast.info('授权仍在等待中')
  } catch (error) {
    device.value = null
    toast.error(error instanceof Error ? error.message : '授权状态检查失败')
  } finally {
    busy.value = false
  }
}

async function importCredential() {
  busy.value = true
  try {
    let credential: Record<string, unknown> | undefined
    if (credentialText.value) {
      const parsed = JSON.parse(credentialText.value)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('CPA OAuth JSON 必须是对象')
      }
      credential = parsed as Record<string, unknown>
    }
    await grokOAuthAccountsApi.importCredential({
      credential,
      access_token: accessToken.value || undefined,
      refresh_token: refreshToken.value || undefined,
    })
    credentialText.value = ''
    accessToken.value = ''
    refreshToken.value = ''
    showImport.value = false
    toast.success('Grok Build OAuth 凭据已导入')
    await loadAccounts()
    emit('changed')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'OAuth 凭据导入失败')
  } finally {
    busy.value = false
  }
}

async function refreshAccount(id: string) {
  busy.value = true
  try {
    await grokOAuthAccountsApi.refresh(id)
    toast.success('OAuth Token 已刷新')
    await loadAccounts()
    emit('changed')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'OAuth Token 刷新失败')
  } finally {
    busy.value = false
  }
}

async function syncModels(id: string) {
  busy.value = true
  try {
    await grokOAuthAccountsApi.syncModels(id)
    toast.success('可用模型已更新')
    await loadAccounts()
    emit('changed')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '模型探测失败')
  } finally {
    busy.value = false
  }
}

async function setDisabled(account: GrokOAuthAccount) {
  busy.value = true
  try {
    await grokOAuthAccountsApi.setDisabled([account.id], account.status !== 'disabled')
    await loadAccounts()
    emit('changed')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '账号状态更新失败')
  } finally {
    busy.value = false
  }
}

async function removeAccount(id: string) {
  busy.value = true
  try {
    await grokOAuthAccountsApi.remove([id])
    toast.success('OAuth 账号已删除')
    await loadAccounts()
    emit('changed')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '账号删除失败')
  } finally {
    busy.value = false
  }
}

onMounted(loadAccounts)
onBeforeUnmount(() => {
  if (protocolPollTimer !== null) window.clearTimeout(protocolPollTimer)
})

defineExpose({ loadAccounts, refreshAccount, syncModels, setDisabled, removeAccount })
</script>
