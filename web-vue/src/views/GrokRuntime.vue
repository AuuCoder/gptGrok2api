<template>
  <div class="space-y-6">
    <PagePanel class="space-y-5">
      <PanelHeader title="Grok Runtime" align="start">
        <template #copy>
          <p class="mt-1 text-xs text-muted-foreground">
            最近更新：{{ lastUpdatedText }}
          </p>
        </template>
        <template #actions>
          <Button size="sm" variant="outline" :disabled="loading" @click="openLegacyAdmin">
            <Icon icon="lucide:external-link" class="h-3.5 w-3.5" />
            高级管理
          </Button>
          <Button size="sm" variant="outline" :disabled="loading" @click="loadRuntime({ toastOnError: true })">
            <Icon icon="lucide:refresh-cw" class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />
            {{ loading ? '刷新中...' : '刷新' }}
          </Button>
        </template>
      </PanelHeader>

      <MetricStrip
        :items="runtimeMetricItems"
        columns-class="grid-cols-2 md:grid-cols-3 xl:grid-cols-5"
        density="compact"
      />

      <StateBlock
        v-if="loadError"
        compact
        dashed
        title="部分 Runtime 数据未加载"
        :description="loadError"
      />
    </PagePanel>

    <PagePanel class="space-y-4">
      <PanelHeader title="Grok 账号池" align="start">
        <template #copy>
          <p class="mt-1 text-xs text-muted-foreground">{{ tokenSummaryText }}</p>
        </template>
      </PanelHeader>

      <PageLoadingState
        v-if="loading && !tokens.length"
        title="正在读取 Grok Token"
        description="正在加载内置运行池状态。"
        compact
        dashed
      />

      <TableShell v-else-if="tokens.length">
        <table class="w-full min-w-[68rem] text-left text-xs">
          <thead class="bg-muted/30 text-muted-foreground">
            <tr>
              <th class="px-4 py-3 font-medium">Token</th>
              <th class="px-4 py-3 font-medium">池</th>
              <th class="px-4 py-3 font-medium">状态</th>
              <th class="px-4 py-3 font-medium">额度</th>
              <th class="px-4 py-3 font-medium">调用</th>
              <th class="px-4 py-3 font-medium">最近使用</th>
              <th class="px-4 py-3 font-medium">标签</th>
              <th class="px-4 py-3 text-right font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in tokens" :key="item.token" class="border-t border-border transition-colors hover:bg-muted/20">
              <td class="max-w-[16rem] px-4 py-3 align-middle">
                <div class="flex items-center gap-2">
                  <span class="truncate font-mono text-xs text-foreground">{{ tokenPreview(item.token) }}</span>
                  <Button
                    size="xs"
                    variant="ghost"
                    icon-only
                    root-class="h-6 w-6 shrink-0"
                    title="复制完整 Token"
                    @click="copyToken(item.token)"
                  >
                    <Icon icon="lucide:copy" class="h-3.5 w-3.5" />
                  </Button>
                </div>
              </td>
              <td class="px-4 py-3 align-middle font-mono text-muted-foreground">{{ item.pool || 'basic' }}</td>
              <td class="px-4 py-3 align-middle">
                <StateBadge :tone="tokenStatusTone(item.status)" size="xs" shape="rounded" :bordered="false">
                  {{ tokenStatusText(item.status) }}
                </StateBadge>
              </td>
              <td class="max-w-[18rem] px-4 py-3 align-middle font-mono text-[11px] leading-5 text-muted-foreground">
                {{ quotaText(item) }}
              </td>
              <td class="px-4 py-3 align-middle font-mono tabular-nums text-muted-foreground">
                {{ usageText(item) }}
              </td>
              <td class="whitespace-nowrap px-4 py-3 align-middle text-muted-foreground">{{ formatTimestamp(item.last_used_at) }}</td>
              <td class="max-w-[12rem] px-4 py-3 align-middle">
                <div v-if="item.tags?.length" class="flex flex-wrap gap-1">
                  <StateBadge v-for="tag in item.tags" :key="tag" size="xs" shape="rounded" :bordered="false">
                    {{ tag }}
                  </StateBadge>
                </div>
                <span v-else class="text-muted-foreground">-</span>
              </td>
              <td class="px-4 py-3 text-right align-middle">
                <div class="flex justify-end gap-1">
                  <Button
                    size="xs"
                    variant="outline"
                    icon-only
                    root-class="h-7 w-7"
                    :disabled="isTokenBusy(item.token)"
                    :title="isTokenAction(item.token, 'refresh') ? '正在刷新...' : '刷新状态和额度'"
                    @click="refreshToken(item)"
                  >
                    <Icon icon="lucide:refresh-cw" class="h-3.5 w-3.5" :class="isTokenAction(item.token, 'refresh') ? 'animate-spin' : ''" />
                  </Button>
                  <Button
                    size="xs"
                    variant="outline"
                    icon-only
                    root-class="h-7 w-7"
                    :disabled="isTokenBusy(item.token)"
                    :title="isTokenAction(item.token, 'disabled') ? '正在更新...' : (isTokenDisabled(item) ? '恢复账号' : '禁用账号')"
                    @click="toggleTokenDisabled(item)"
                  >
                    <Icon :icon="isTokenDisabled(item) ? 'lucide:circle-play' : 'lucide:circle-pause'" class="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    size="xs"
                    variant="outline"
                    icon-only
                    root-class="h-7 w-7 text-rose-600 hover:text-rose-700"
                    :disabled="isTokenBusy(item.token)"
                    :title="isTokenAction(item.token, 'delete') ? '正在删除...' : '删除账号'"
                    @click="removeToken(item)"
                  >
                    <Icon icon="lucide:trash-2" class="h-3.5 w-3.5" />
                  </Button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </TableShell>

      <StateBlock
        v-else
        compact
        dashed
        title="暂无 Grok Runtime Token"
        description="注册成功的 Grok 账号加入运行池后会显示在这里。"
      />
    </PagePanel>

    <PagePanel class="space-y-4">
      <PanelHeader title="Runtime 配置" align="start">
        <template #copy>
          <p class="mt-1 text-xs text-muted-foreground">
            {{ configDirty ? '存在未保存修改' : 'JSON 配置已同步' }}
          </p>
        </template>
        <template #actions>
          <Button
            size="sm"
            variant="outline"
            :disabled="loading || savingConfig"
            @click="loadRuntime({ forceConfig: true, toastOnError: true })"
          >
            重载配置
          </Button>
          <Button size="sm" variant="primary" :disabled="savingConfig || !configLoaded" @click="saveConfig">
            {{ savingConfig ? '保存中...' : '保存配置' }}
          </Button>
        </template>
      </PanelHeader>

      <textarea
        v-model="configText"
        rows="20"
        class="ui-textarea-sm min-h-[24rem] resize-y font-mono leading-5"
        aria-label="Grok Runtime JSON 配置"
        spellcheck="false"
      ></textarea>
      <p v-if="configError" class="text-xs text-rose-600">{{ configError }}</p>
    </PagePanel>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Icon } from '@iconify/vue'
import { Button } from 'nanocat-ui'

import MetricStrip from '@/components/ai/MetricStrip.vue'
import PageLoadingState from '@/components/ai/PageLoadingState.vue'
import PagePanel from '@/components/ai/PagePanel.vue'
import PanelHeader from '@/components/ai/PanelHeader.vue'
import StateBadge from '@/components/ai/StateBadge.vue'
import StateBlock from '@/components/ai/StateBlock.vue'
import TableShell from '@/components/ai/TableShell.vue'
import { getAuthToken } from '@/api/client'
import {
  grokRuntimeApi,
  type GrokRuntimeConfig,
  type GrokRuntimeStatus,
  type GrokRuntimeStorage,
  type GrokRuntimeToken,
} from '@/api/grokRuntime'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'

defineOptions({ name: 'GrokRuntime' })

type TokenAction = 'refresh' | 'disabled' | 'delete'
type LoadOptions = {
  forceConfig?: boolean
  toastOnError?: boolean
}

const toast = useToast()
const confirmDialog = useConfirmDialog()
const loading = ref(false)
const savingConfig = ref(false)
const configLoaded = ref(false)
const loadError = ref('')
const configError = ref('')
const runtimeStatus = ref<GrokRuntimeStatus | null>(null)
const runtimeStorage = ref<GrokRuntimeStorage | null>(null)
const tokens = ref<GrokRuntimeToken[]>([])
const configText = ref('')
const configBaseline = ref('')
const lastUpdatedAt = ref(0)
const activeToken = ref('')
const activeTokenAction = ref<TokenAction | null>(null)

const configDirty = computed(() => configText.value !== configBaseline.value)
const lastUpdatedText = computed(() => {
  if (!lastUpdatedAt.value) return '未获取'
  return new Date(lastUpdatedAt.value).toLocaleString('zh-CN', { hour12: false })
})
const activeTokenCount = computed(() => tokens.value.filter((item) => String(item.status || '').toLowerCase() === 'active').length)
const tokenSummaryText = computed(() => `共 ${tokens.value.length} 个 Token，正常 ${activeTokenCount.value} 个`)
const runtimeMetricItems = computed(() => [
  {
    key: 'status',
    label: '运行状态',
    value: runtimeStatusText(runtimeStatus.value?.status),
    meta: `修订 ${runtimeStatus.value?.revision ?? '-'}`,
    icon: 'lucide:activity',
    iconBgClass: 'bg-emerald-500/10',
    iconClass: 'text-emerald-600',
  },
  {
    key: 'accounts',
    label: '运行目录',
    value: runtimeStatus.value?.size ?? tokens.value.length,
    meta: `Token ${tokens.value.length}`,
    icon: 'lucide:users-round',
    iconBgClass: 'bg-cyan-500/10',
    iconClass: 'text-cyan-600',
  },
  {
    key: 'storage',
    label: '存储后端',
    value: runtimeStorage.value?.type || '未获取',
    meta: 'Runtime repository',
    icon: 'lucide:database',
    iconBgClass: 'bg-amber-500/10',
    iconClass: 'text-amber-600',
  },
  {
    key: 'strategy',
    label: '调度策略',
    value: strategyText(runtimeStatus.value?.selection_strategy),
    meta: runtimeStatus.value?.selection_strategy || '-',
    icon: 'lucide:shuffle',
    iconBgClass: 'bg-violet-500/10',
    iconClass: 'text-violet-600',
  },
  {
    key: 'active',
    label: '正常账号',
    value: activeTokenCount.value,
    meta: `共 ${tokens.value.length} 个`,
    icon: 'lucide:circle-check-big',
    iconBgClass: 'bg-sky-500/10',
    iconClass: 'text-sky-600',
  },
])

function runtimeStatusText(value: unknown) {
  const status = String(value || '').trim().toLowerCase()
  if (!status) return '未获取'
  if (status === 'ok') return '正常'
  return status
}

function strategyText(value: unknown) {
  const strategy = String(value || '').trim().toLowerCase()
  if (strategy === 'quota') return '按额度'
  if (strategy === 'random') return '随机'
  return strategy || '未获取'
}

function tokenStatusText(value: unknown) {
  const status = String(value || '').trim().toLowerCase()
  if (status === 'active') return '正常'
  if (status === 'cooling' || status === 'rate_limited') return '限流'
  if (status === 'disabled') return '已禁用'
  if (status === 'invalid' || status === 'expired') return '失效'
  return status || '未知'
}

function tokenStatusTone(value: unknown): 'success' | 'warning' | 'danger' | 'muted' {
  const status = String(value || '').trim().toLowerCase()
  if (status === 'active') return 'success'
  if (status === 'cooling' || status === 'rate_limited') return 'warning'
  if (status === 'disabled') return 'muted'
  return 'danger'
}

function tokenPreview(token: string) {
  const value = String(token || '').trim()
  if (!value) return '-'
  if (value.length <= 20) return value
  return `${value.slice(0, 8)}...${value.slice(-8)}`
}

function quotaText(item: GrokRuntimeToken) {
  const labels = {
    auto: 'A',
    fast: 'F',
    expert: 'E',
    heavy: 'H',
    console: 'C',
  } as const
  const entries = (Object.keys(labels) as Array<keyof typeof labels>)
    .map((key) => {
      const quota = item.quota?.[key]
      if (!quota) return ''
      const remaining = Math.max(0, Number(quota.remaining) || 0)
      const total = Math.max(0, Number(quota.total) || 0)
      return `${labels[key]} ${remaining}/${total || '-'}`
    })
    .filter(Boolean)
  return entries.join(' · ') || '-'
}

function usageText(item: GrokRuntimeToken) {
  const success = Math.max(0, Number(item.use_count) || 0)
  const failed = Math.max(0, Number(item.fail_count) || 0)
  return `${success} / ${failed}`
}

function formatTimestamp(value: unknown) {
  const raw = String(value || '').trim()
  if (!raw) return '-'
  const numeric = Number(raw)
  const date = Number.isFinite(numeric) && numeric > 0
    ? new Date(numeric > 10_000_000_000 ? numeric : numeric * 1000)
    : new Date(raw)
  if (Number.isNaN(date.getTime())) return raw
  return date.toLocaleString('zh-CN', { hour12: false })
}

function isTokenDisabled(item: GrokRuntimeToken) {
  return String(item.status || '').trim().toLowerCase() === 'disabled'
}

function isTokenBusy(token: string) {
  return Boolean(activeToken.value) && activeToken.value === token
}

function isTokenAction(token: string, action: TokenAction) {
  return activeToken.value === token && activeTokenAction.value === action
}

function applyConfig(config: GrokRuntimeConfig) {
  const text = JSON.stringify(config, null, 2)
  configText.value = text
  configBaseline.value = text
  configLoaded.value = true
  configError.value = ''
}

async function loadRuntime(options: LoadOptions = {}) {
  if (loading.value) return
  loading.value = true
  loadError.value = ''
  const [statusResult, storageResult, tokensResult, configResult] = await Promise.allSettled([
    grokRuntimeApi.getStatus(),
    grokRuntimeApi.getStorage(),
    grokRuntimeApi.getTokens(),
    grokRuntimeApi.getConfig(),
  ])

  if (statusResult.status === 'fulfilled') runtimeStatus.value = statusResult.value
  if (storageResult.status === 'fulfilled') runtimeStorage.value = storageResult.value
  if (tokensResult.status === 'fulfilled') {
    tokens.value = Array.isArray(tokensResult.value.tokens) ? tokensResult.value.tokens : []
  }
  if (
    configResult.status === 'fulfilled'
    && (options.forceConfig || !configLoaded.value || !configDirty.value)
  ) {
    applyConfig(configResult.value)
  }

  const failures = [statusResult, storageResult, tokensResult, configResult]
    .flatMap((result) => result.status === 'rejected' ? [errorMessage(result.reason)] : [])
    .filter(Boolean)
  if (failures.length) {
    loadError.value = Array.from(new Set(failures)).join('；')
    if (options.toastOnError) toast.error(loadError.value, 'Grok Runtime 加载失败')
  }
  lastUpdatedAt.value = Date.now()
  loading.value = false
}

async function refreshToken(item: GrokRuntimeToken) {
  if (isTokenBusy(item.token)) return
  activeToken.value = item.token
  activeTokenAction.value = 'refresh'
  try {
    const result = await grokRuntimeApi.refreshToken(item.token)
    const failed = Number(result.summary?.fail || 0)
    if (failed) toast.warning('Grok Token 刷新完成，但上游未返回有效额度。')
    else toast.success('Grok Token 状态和额度已刷新。')
    await loadRuntime()
  } catch (error) {
    toast.error(`刷新失败：${errorMessage(error)}`)
  } finally {
    activeToken.value = ''
    activeTokenAction.value = null
  }
}

async function toggleTokenDisabled(item: GrokRuntimeToken) {
  if (isTokenBusy(item.token)) return
  const disabled = !isTokenDisabled(item)
  const confirmed = await confirmDialog.ask({
    title: disabled ? '禁用 Grok Token' : '恢复 Grok Token',
    message: disabled
      ? '禁用后该 Token 不会参与 Grok 请求分配。'
      : '恢复后该 Token 会重新参与 Grok 请求分配。',
    confirmText: disabled ? '禁用' : '恢复',
    cancelText: '取消',
  })
  if (!confirmed) return

  activeToken.value = item.token
  activeTokenAction.value = 'disabled'
  try {
    await grokRuntimeApi.setTokenDisabled(item.token, disabled)
    toast.success(disabled ? 'Grok Token 已禁用。' : 'Grok Token 已恢复。')
    await loadRuntime()
  } catch (error) {
    toast.error(`操作失败：${errorMessage(error)}`)
  } finally {
    activeToken.value = ''
    activeTokenAction.value = null
  }
}

async function removeToken(item: GrokRuntimeToken) {
  if (isTokenBusy(item.token)) return
  const confirmed = await confirmDialog.ask({
    title: '删除 Grok Token',
    message: `确认从 Grok Runtime 中删除 ${tokenPreview(item.token)} 吗？删除后无法恢复。`,
    confirmText: '删除',
    cancelText: '取消',
  })
  if (!confirmed) return

  activeToken.value = item.token
  activeTokenAction.value = 'delete'
  try {
    await grokRuntimeApi.removeToken(item.token)
    toast.success('Grok Token 已删除。')
    await loadRuntime()
  } catch (error) {
    toast.error(`删除失败：${errorMessage(error)}`)
  } finally {
    activeToken.value = ''
    activeTokenAction.value = null
  }
}

async function copyToken(token: string) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(token)
    } else {
      const input = document.createElement('textarea')
      input.value = token
      input.setAttribute('readonly', 'readonly')
      input.style.position = 'fixed'
      input.style.left = '-9999px'
      document.body.appendChild(input)
      input.select()
      const copied = document.execCommand('copy')
      document.body.removeChild(input)
      if (!copied) throw new Error('copy failed')
    }
    toast.success('Token 已复制。')
  } catch {
    toast.error('复制失败，请检查浏览器权限。')
  }
}

async function saveConfig() {
  let nextConfig: GrokRuntimeConfig
  try {
    const parsed: unknown = JSON.parse(configText.value)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('配置根节点必须是 JSON 对象')
    }
    nextConfig = parsed as GrokRuntimeConfig
  } catch (error) {
    configError.value = `配置格式无效：${errorMessage(error, 'JSON 解析失败')}`
    return
  }

  savingConfig.value = true
  configError.value = ''
  try {
    const result = await grokRuntimeApi.updateConfig(nextConfig)
    toast.success(result.message || 'Grok Runtime 配置已保存。')
    configBaseline.value = configText.value
    await loadRuntime({ forceConfig: true })
  } catch (error) {
    configError.value = `保存失败：${errorMessage(error)}`
    toast.error(configError.value)
  } finally {
    savingConfig.value = false
  }
}

function bytesToBase64(bytes: Uint8Array) {
  let value = ''
  bytes.forEach((byte) => { value += String.fromCharCode(byte) })
  return window.btoa(value)
}

async function encodeLegacyAdminSession(token: string) {
  const secret = new TextEncoder().encode('grok2api-admin-key')
  const source = new TextEncoder().encode(token)
  if (!window.crypto?.subtle) {
    const output = new Uint8Array(source.length)
    source.forEach((byte, index) => { output[index] = byte ^ secret[index % secret.length] })
    return `enc:xor:${bytesToBase64(output)}`
  }

  const salt = window.crypto.getRandomValues(new Uint8Array(16))
  const iv = window.crypto.getRandomValues(new Uint8Array(12))
  const keyMaterial = await window.crypto.subtle.importKey('raw', secret, 'PBKDF2', false, ['deriveKey'])
  const key = await window.crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt, iterations: 100000, hash: 'SHA-256' },
    keyMaterial,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt'],
  )
  const ciphertext = new Uint8Array(await window.crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, source))
  return `enc:v1:${bytesToBase64(salt)}:${bytesToBase64(iv)}:${bytesToBase64(ciphertext)}`
}

function openLegacyAdmin() {
  const legacyWindow = window.open('about:blank', '_blank')
  if (!legacyWindow) {
    toast.warning('浏览器阻止了新窗口，请允许弹窗后重试。')
    return
  }

  void (async () => {
    try {
      const token = getAuthToken()
      if (!token) throw new Error('当前管理员登录态不存在')
      window.localStorage.setItem('grok2api_admin_key', await encodeLegacyAdminSession(token))
    } catch {
      toast.warning('无法同步旧版登录态，请在旧版页面重新登录。')
    } finally {
      legacyWindow.location.replace('/admin/account')
    }
  })()
}

onMounted(() => {
  void loadRuntime()
})
</script>
