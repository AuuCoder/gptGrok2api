<template>
  <div class="register-page">
    <PagePanel class="space-y-4">
      <PanelHeader title="注册账号" align="start">
        <template #actions>
          <StateBadge :tone="registerConfig?.enabled ? 'success' : 'muted'" shape="rounded" size="sm">
            {{ registerConfig?.enabled ? '运行中' : '未启动' }}
          </StateBadge>
          <Button
            size="sm"
            variant="primary"
            :disabled="legacySaving || !registerConfig || registerConfig.enabled"
            @click="saveLegacyConfig"
          >
            保存配置
          </Button>
        </template>
      </PanelHeader>

      <PageLoadingState
        v-if="legacyLoading && !registerConfig"
        title="正在加载注册配置"
        description="读取邮箱来源、任务参数和运行状态。"
      />

      <div v-else-if="registerConfig" class="register-content">
        <div class="register-layout">
          <div class="register-config-column">
            <RegisterTaskSettingsPanel
              :config="registerConfig"
              :checkout-saving="legacySaving"
              :sub2api-sync-saving="sub2apiSyncSaving"
              :sub2api-sync-save-status="sub2apiSyncSaveStatus"
              :sub2api-sync-save-message="sub2apiSyncSaveMessage"
              :proxy-mode="registerProxyMode"
              :selected-proxy-group-id="selectedRegisterProxyGroupId"
              :custom-proxy-input="customRegisterProxyInput"
              :proxy-group-groups="registerProxyGroupGroups"
              :proxy-hint="registerProxyHint"
              @update-target="setRegisterTarget"
              @update-proxy-mode="setRegisterProxyMode"
              @select-proxy-group="selectRegisterProxyGroup"
              @update-custom-proxy="setCustomRegisterProxyInput"
              @save-checkout="saveLegacyConfig"
              @save-sub2api-sync="saveSub2APISyncConfig"
              @sub2api-sync-changed="resetSub2APISyncSaveStatus"
            />

            <FormSection title="邮箱来源" density="roomy">
              <template #actions>
                <MetaChip v-if="enabledProviderIssueCount" size="xs" tone="danger">
                  缺失 {{ enabledProviderIssueCount }}
                </MetaChip>
                <MetaChip size="xs" tone="muted">已启用 {{ enabledProviderCount }} / {{ registerProviders.length }}</MetaChip>
                <Button
                  v-if="disabledProviderCount"
                  size="sm"
                  variant="ghost"
                  :aria-expanded="showDisabledProviders"
                  aria-controls="disabled-register-providers"
                  @click="showDisabledProviders = !showDisabledProviders"
                >
                  {{ showDisabledProviders ? '收起未启用' : `查看未启用 (${disabledProviderCount})` }}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  :disabled="registerConfig.enabled"
                  @click="addProvider"
                >
                  添加来源
                </Button>
              </template>

              <div id="disabled-register-providers" class="register-provider-list">
                <div v-if="!visibleRegisterProviderEntries.length" class="register-provider-empty">
                  <template v-if="disabledProviderCount">
                    暂无已启用邮箱来源。展开未启用来源后可编辑或重新启用。
                  </template>
                  <template v-else>
                    暂无邮箱来源，请添加来源后继续。
                  </template>
                </div>
                <RegisterProviderCard
                  v-for="({ provider, index }) in visibleRegisterProviderEntries"
                  :key="providerKey(provider, index)"
                  :provider="provider"
                  :index="index"
                  :provider-count="registerProviders.length"
                  :disabled="registerConfig.enabled"
                  :saving="legacySaving"
                  :outlook-pool-action-items="outlookPoolActionItems"
                  :gpt-mail="registerProviderGptMailUi"
                  @update-type="updateProviderType"
                  @update-field="updateProviderField"
                  @update-array="updateProviderArray"
                  @delete="deleteProvider"
                  @check-gptmail="checkGptMailStatus"
                  @outlook-action="handleOutlookPoolAction"
                />
              </div>
            </FormSection>
          </div>

          <RegisterRuntimePanel
            :target="registerTarget"
            :enabled="registerConfig.enabled"
            :saving="legacySaving"
            :export-busy="grokExportBusy"
            :action-disabled="registerActionDisabled"
            :runtime-hint="registerRuntimeHint"
            :metric-items="registerMetricItems"
            :runtime-log-lines="runtimeLogLines"
            :checkout-log-lines="checkoutRuntimeLogLines"
            @toggle-task="toggleLegacyTask"
            @reset-stats="resetLegacyStats"
            @export-grok="exportGrokAccounts"
          >
            <FormSection
              v-if="hasCheckoutRuntime"
              id="checkout-runtime"
              title="提链任务"
              density="roomy"
              surface="plain"
              class="register-link-tasks"
            >
              <template #actions>
                <MetaChip size="xs" tone="muted">{{ checkoutTasks.length }} 个任务</MetaChip>
                <Button
                  v-if="checkoutRetriesActive"
                  size="sm"
                  variant="outline"
                  :disabled="checkoutRetryStopping"
                  @click="stopCheckoutRetries"
                >
                  <Icon icon="lucide:square" class="h-3.5 w-3.5" />
                  {{ checkoutRetryStopping ? '结束中...' : '结束提链' }}
                </Button>
                <Button
                  v-if="clearableCheckoutTaskCount > 0"
                  size="sm"
                  variant="outline"
                  :disabled="checkoutHistoryClearing"
                  @click="clearCheckoutHistory"
                >
                  <Icon icon="lucide:trash-2" class="h-3.5 w-3.5" />
                  {{ checkoutHistoryClearing ? '清空中...' : '清空历史' }}
                </Button>
              </template>

              <CheckoutTaskTable
                :tasks="checkoutTasks"
                @copy-payment-link="copyCheckoutPaymentLink"
              />
            </FormSection>
          </RegisterRuntimePanel>
        </div>
      </div>
    </PagePanel>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { Icon } from '@iconify/vue'
import { useRoute, useRouter } from 'vue-router'
import { Button } from 'nanocat-ui'
import { registerApi } from '@/api/register'
import FormSection from '@/components/ai/FormSection.vue'
import MetaChip from '@/components/ai/MetaChip.vue'
import PageLoadingState from '@/components/ai/PageLoadingState.vue'
import PagePanel from '@/components/ai/PagePanel.vue'
import PanelHeader from '@/components/ai/PanelHeader.vue'
import StateBadge from '@/components/ai/StateBadge.vue'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { usePageRuntime } from '@/composables/usePageRuntime'
import { useToast } from '@/composables/useToast'
import { saveBlob } from '@/lib/downloads'
import RegisterProviderCard from '@/views/register/RegisterProviderCard.vue'
import CheckoutTaskTable from '@/views/register/CheckoutTaskTable.vue'
import RegisterRuntimePanel from '@/views/register/RegisterRuntimePanel.vue'
import RegisterTaskSettingsPanel from '@/views/register/RegisterTaskSettingsPanel.vue'
import {
  defaultRegisterConfig,
  enabledRegisterProviderCount as buildEnabledRegisterProviderCount,
  formatClock,
  grokRequirementMessages,
  normalizeRegisterTarget,
  providerForRegisterTarget,
  registerActionDisabled as buildRegisterActionDisabled,
  registerMetricItems as buildRegisterMetricItems,
  registerProviderIssueCount as buildRegisterProviderIssueCount,
  registerRuntimeHint as buildRegisterRuntimeHint,
  registerRuntimeLogLines as buildRegisterRuntimeLogLines,
  sub2apiSyncRequirementMessages,
} from '@/views/register/registerProviderView'
import { useRegisterConfigRuntime } from '@/views/register/registerConfigRuntime'
import { useRegisterGptMailRuntime } from '@/views/register/registerGptMailRuntime'
import { useRegisterLiveRuntime } from '@/views/register/registerLiveRuntime'
import { useRegisterOutlookPoolRuntime } from '@/views/register/registerOutlookPoolRuntime'
import { useRegisterProviderRuntime } from '@/views/register/registerProviderRuntime'

defineOptions({ name: 'Register' })

const toast = useToast()
const confirmDialog = useConfirmDialog()
const route = useRoute()
const router = useRouter()
const pageRuntime = usePageRuntime('register')
const grokExportBusy = ref(false)
const checkoutRetryStopping = ref(false)
const checkoutHistoryClearing = ref(false)

const registerConfigRuntime = useRegisterConfigRuntime({
  runtime: pageRuntime,
  confirm: confirmDialog.ask,
  notifySuccess: (message) => toast.success(message),
  notifyError: (message) => toast.error(message),
  startLiveUpdates: () => startLiveUpdates(),
})
const legacyLoading = registerConfigRuntime.loading
const legacySaving = registerConfigRuntime.saving
const sub2apiSyncSaving = registerConfigRuntime.sub2apiSyncSaving
const sub2apiSyncSaveStatus = registerConfigRuntime.sub2apiSyncSaveStatus
const sub2apiSyncSaveMessage = registerConfigRuntime.sub2apiSyncSaveMessage
const registerConfig = registerConfigRuntime.config
const registerProviders = registerConfigRuntime.providers
const registerProxyMode = registerConfigRuntime.proxyMode
const selectedRegisterProxyGroupId = registerConfigRuntime.selectedProxyGroupId
const customRegisterProxyInput = registerConfigRuntime.customProxyInput
const registerProxyGroupGroups = registerConfigRuntime.proxyGroupGroups
const registerProxyHint = registerConfigRuntime.proxyHint
const applyRegisterConfig = registerConfigRuntime.applyConfig
const applyRegisterRuntimeConfig = registerConfigRuntime.applyRuntimeConfig
const loadRegisterConfig = registerConfigRuntime.loadConfig
const loadRegisterRuntimeConfig = registerConfigRuntime.loadRuntimeConfig
const loadProxyGroups = registerConfigRuntime.loadProxyGroups
const saveLegacyConfig = registerConfigRuntime.saveConfig
const saveSub2APISyncConfig = registerConfigRuntime.saveSub2APISyncConfig
const resetSub2APISyncSaveStatus = registerConfigRuntime.resetSub2APISyncSaveStatus
const toggleLegacyTask = registerConfigRuntime.toggleTask
const resetLegacyStats = registerConfigRuntime.resetStats
const setRegisterTarget = registerConfigRuntime.setTarget
const setRegisterProxyMode = registerConfigRuntime.setProxyMode
const selectRegisterProxyGroup = registerConfigRuntime.selectProxyGroup
const setCustomRegisterProxyInput = registerConfigRuntime.setCustomProxyInput
const gptMailRuntime = useRegisterGptMailRuntime({
  runtime: pageRuntime,
  providers: registerProviders,
  notifySuccess: (message) => toast.success(message),
  notifyError: (message) => toast.error(message),
})
const clearGptMailState = gptMailRuntime.clearState
const clearAllGptMailRefreshTimers = gptMailRuntime.clearAllRefreshTimers
const clearAllGptMailStates = gptMailRuntime.clearAllStates
const pruneGptMailStates = gptMailRuntime.pruneStates
const gptMailStatusByIndex = gptMailRuntime.statusByIndex
const gptMailStatusBusy = gptMailRuntime.statusBusy
const gptMailStatusTone = gptMailRuntime.statusTone
const gptMailStatusTitle = gptMailRuntime.statusTitle
const gptMailRemainingText = gptMailRuntime.remainingText
const gptMailResetText = gptMailRuntime.resetText
const gptMailStatusHint = gptMailRuntime.statusHint
const checkGptMailStatus = gptMailRuntime.checkStatus
const startGptMailClock = gptMailRuntime.startClock
const stopGptMailClock = gptMailRuntime.stopClock
const registerProviderGptMailUi = {
  statusByIndex: gptMailStatusByIndex,
  statusBusy: gptMailStatusBusy,
  statusTone: gptMailStatusTone,
  statusTitle: gptMailStatusTitle,
  remainingText: gptMailRemainingText,
  resetText: gptMailResetText,
  statusHint: gptMailStatusHint,
}
const outlookPoolRuntime = useRegisterOutlookPoolRuntime({
  saving: legacySaving,
  confirm: confirmDialog.ask,
  applyConfig: (config) => applyRegisterConfig(config),
  notifySuccess: (message) => toast.success(message),
  notifyError: (message) => toast.error(message),
})
const outlookPoolActionItems = outlookPoolRuntime.outlookPoolActionItems
const handleOutlookPoolAction = outlookPoolRuntime.handleAction
function hasActiveCheckoutRetries() {
  if (registerConfig.value?.checkout_retries_active === true) return true
  return Array.isArray(registerConfig.value?.checkout_tasks) && registerConfig.value.checkout_tasks.some((task) => (
    ['queued', 'running', 'retrying', 'pending'].includes(String(task.status || '').trim().toLowerCase())
  ))
}
const liveRuntime = useRegisterLiveRuntime({
  runtime: pageRuntime,
  getAuthToken: registerConfigRuntime.authToken,
  loadConfig: () => loadRegisterRuntimeConfig(),
  applyConfig: (config) => applyRegisterRuntimeConfig(config),
  isTaskEnabled: registerConfigRuntime.isTaskEnabled,
  hasActiveCheckoutRetries,
})
const startLiveUpdates = liveRuntime.startLiveUpdates
registerConfigRuntime.onConfigApplied(() => pruneGptMailStates())
const enabledProviderCount = computed(() => buildEnabledRegisterProviderCount(registerProviders.value))
const disabledProviderCount = computed(() => registerProviders.value.length - enabledProviderCount.value)
const showDisabledProviders = ref(false)
const visibleRegisterProviderEntries = computed(() => registerProviders.value
  .map((provider, index) => ({ provider, index }))
  .filter(({ provider }) => showDisabledProviders.value || provider.enable !== false))
const enabledProviderIssueCount = computed(() => buildRegisterProviderIssueCount(registerProviders.value))
const grokConfigIssueCount = computed(() => grokRequirementMessages(registerConfig.value).length)
const sub2apiSyncIssueCount = computed(() => sub2apiSyncRequirementMessages(registerConfig.value).length)
const registerIssueCount = computed(() => (
  enabledProviderIssueCount.value + grokConfigIssueCount.value + sub2apiSyncIssueCount.value
))
const registerTarget = computed(() => normalizeRegisterTarget(registerConfig.value?.target))
const registerActionDisabled = computed(() => buildRegisterActionDisabled(
  registerConfig.value,
  legacySaving.value,
  enabledProviderCount.value,
  registerIssueCount.value,
))
const legacyStats = computed(() => ({ ...defaultRegisterConfig.stats, ...(registerConfig.value?.stats || {}) }))
const legacyLogs = computed(() => (registerConfig.value?.logs || []).slice(-120))
const checkoutLogs = computed(() => (registerConfig.value?.checkout_logs || []).slice(-120))
const checkoutTasks = computed(() => Array.isArray(registerConfig.value?.checkout_tasks)
  ? [...registerConfig.value.checkout_tasks]
  : [])
const clearableCheckoutTaskCount = computed(() => checkoutTasks.value.filter((task) => (
  !['queued', 'running', 'retrying', 'pending'].includes(String(task.status || '').trim().toLowerCase())
)).length)
const checkoutRetriesActive = computed(hasActiveCheckoutRetries)
const hasCheckoutRuntime = computed(() => (
  registerTarget.value === 'openai' ||
  checkoutRetriesActive.value ||
  checkoutTasks.value.length > 0
))
const registerRuntimeHint = computed(() => buildRegisterRuntimeHint(
  registerConfig.value,
  enabledProviderCount.value,
  registerIssueCount.value,
))

const registerMetricItems = computed(() => buildRegisterMetricItems(
  legacyStats.value,
  registerConfig.value?.threads || 0,
  registerTarget.value,
))

const runtimeLogLines = computed(() => buildRegisterRuntimeLogLines(legacyLogs.value, formatClock))
const checkoutRuntimeLogLines = computed(() => buildRegisterRuntimeLogLines(checkoutLogs.value, formatClock))
const providerRuntime = useRegisterProviderRuntime({
  config: registerConfig,
  providers: registerProviders,
  confirm: confirmDialog.ask,
  clearGptMailState,
  clearAllGptMailStates,
})
const providerKey = providerRuntime.providerKey
const updateProviderTypeDraft = providerRuntime.updateProviderType
const updateProviderFieldDraft = providerRuntime.updateProviderField
const addProvider = providerRuntime.addProvider
const deleteProvider = providerRuntime.deleteProvider
const updateProviderArray = providerRuntime.updateProviderArray

function updateProviderType(index: number, type: string) {
  updateProviderTypeDraft(index, type)
  if (!registerConfig.value || type !== 'icloud_api') return
  const provider = registerConfig.value.mail.providers?.[index]
  if (!provider) return
  registerConfig.value.mail.providers![index] = providerForRegisterTarget(provider, registerTarget.value)
}

function updateProviderField(index: number, key: string, value: unknown) {
  updateProviderFieldDraft(index, key, value)
  if (key === 'enable' && value === false) showDisabledProviders.value = true
}

function grokExportFilename() {
  const stamp = new Date().toISOString().slice(0, 19).replaceAll(':', '-')
  return `grok-accounts-${stamp}.json`
}

async function exportGrokAccounts() {
  const confirmed = await confirmDialog.ask({
    title: '导出 Grok 账号',
    message: '导出文件包含账号认证信息，请确认在可信环境中保存。',
    confirmText: '导出',
  })
  if (!confirmed) return

  grokExportBusy.value = true
  try {
    const blob = await registerApi.exportGrokAccounts('json')
    if (!blob.size) throw new Error('导出文件为空')
    saveBlob(blob, grokExportFilename())
    toast.success('Grok 账号已导出')
  } catch (error: any) {
    toast.error(error?.message || '导出 Grok 账号失败')
  } finally {
    grokExportBusy.value = false
  }
}

async function stopCheckoutRetries() {
  const confirmed = await confirmDialog.ask({
    title: '停止持续提链',
    message: '将停止所有排队或重试中的 Checkout 提链。正在执行的当前请求结束后不会再开启下一轮。',
    confirmText: '停止提链',
    cancelText: '继续运行',
  })
  if (!confirmed) return

  checkoutRetryStopping.value = true
  try {
    const response = await registerApi.stopCheckoutRetries()
    applyRegisterConfig(response.register)
    toast.success('持续提链已停止')
  } catch (error: any) {
    toast.error(error?.message || '停止持续提链失败')
  } finally {
    checkoutRetryStopping.value = false
  }
}

async function clearCheckoutHistory() {
  const confirmed = await confirmDialog.ask({
    title: '清空提链历史',
    message: `将删除 ${clearableCheckoutTaskCount.value} 条已成功、已失败或已取消的提链记录，不影响运行中任务。`,
    confirmText: '清空历史',
  })
  if (!confirmed) return

  checkoutHistoryClearing.value = true
  try {
    const response = await registerApi.clearCheckoutHistory()
    applyRegisterRuntimeConfig(response.register)
    toast.success(`已清空 ${response.removed || 0} 条提链历史`)
  } catch (error: any) {
    toast.error(error?.message || '清空提链历史失败')
  } finally {
    checkoutHistoryClearing.value = false
  }
}

async function copyCheckoutPaymentLink(value: string) {
  const link = String(value || '').trim()
  if (!link) return
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(link)
    } else {
      const input = document.createElement('textarea')
      input.value = link
      input.setAttribute('readonly', 'readonly')
      input.style.position = 'fixed'
      input.style.left = '-9999px'
      input.style.top = '0'
      document.body.appendChild(input)
      input.focus()
      input.select()
      input.setSelectionRange(0, input.value.length)
      const copied = document.execCommand('copy')
      document.body.removeChild(input)
      if (!copied) throw new Error('copy failed')
    }
    toast.success('支付链接已复制')
  } catch {
    toast.error('复制失败，请手动复制支付链接')
  }
}

async function focusCheckoutRuntime() {
  await nextTick()
  document.getElementById('checkout-runtime')?.scrollIntoView({
    behavior: 'smooth',
    block: 'start',
  })
}

const checkoutFocusHandled = ref(false)

watch(
  () => [
    String(route.query.focus || '').trim().toLowerCase(),
    Boolean(registerConfig.value),
    legacyLoading.value,
  ] as const,
  async ([focus, hasConfig, loading]) => {
    if (checkoutFocusHandled.value || focus !== 'checkout' || !hasConfig || loading) return
    checkoutFocusHandled.value = true
    await focusCheckoutRuntime()

    const nextQuery = { ...route.query }
    delete nextQuery.focus
    await router.replace({ path: route.path, query: nextQuery })
  },
  { flush: 'post', immediate: true },
)

function activateRegisterView(refresh = false) {
  startGptMailClock()
  if (refresh) {
    void Promise.all([loadRegisterConfig(true), loadProxyGroups()])
  }
  startLiveUpdates()
}

function deactivateRegisterView() {
  registerConfigRuntime.invalidate()
  liveRuntime.stop()
  stopGptMailClock()
  clearAllGptMailRefreshTimers()
}

pageRuntime.onActivate(({ initial }) => {
  if (initial) {
    void (async () => {
      startGptMailClock()
      await Promise.all([loadRegisterConfig(), loadProxyGroups()])
      startLiveUpdates()
    })()
    return
  }
  activateRegisterView(true)
})

pageRuntime.onDeactivate(() => {
  deactivateRegisterView()
})

pageRuntime.onHide(() => {
  deactivateRegisterView()
})

pageRuntime.onShow(() => {
  startGptMailClock()
  void loadRegisterConfig(true)
  startLiveUpdates()
})
</script>

<style scoped>
.register-layout {
  display: grid;
  gap: 18px;
}

.register-content {
  display: grid;
  gap: 16px;
}

.register-link-tasks {
  display: flex;
  min-height: 0;
  min-width: 0;
  flex-direction: column;
  gap: 12px;
  scroll-margin-top: 16px;
}

.register-link-tasks :deep(.checkout-task-table-panel) {
  height: auto;
  flex: 1;
}

@media (min-width: 1280px) {
  .register-layout {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    align-items: stretch;
  }
}

.register-config-column {
  min-width: 0;
}

.register-config-column {
  display: grid;
  gap: 16px;
}

.register-provider-list {
  display: grid;
  gap: 14px;
}

.register-provider-empty {
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.5;
}

</style>
