<template>
  <div class="space-y-6">
    <PagePanel v-if="localSettings" class="space-y-5">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="ui-section-title">设置</p>
          <p class="mt-1 text-xs text-muted-foreground">按原版模块分组维护系统配置。</p>
        </div>
        <div class="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" :disabled="settingsStore.isLoading || isSaving" @click="reloadSettings">
            {{ settingsStore.isLoading ? '刷新中...' : '刷新' }}
          </Button>
          <Button size="sm" variant="primary" :disabled="isSaving || !localSettings" @click="handleSave">
            {{ isSaving ? '保存中...' : '保存设置' }}
          </Button>
        </div>
      </div>

      <ConsoleSegmentedTabs v-model="activeSettingsTab" :options="settingsTabs" aria-label="设置分组" />

      <div v-if="activeSettingsTab === 'basic'" class="space-y-4">
        <SurfaceBox density="compact">
          <p class="text-xs leading-5 text-muted-foreground">
            管理员登录密钥继续从部署配置读取，不在此页面展示；如需分发给其他人，请到“用户密钥”创建普通用户密钥。
          </p>
        </SurfaceBox>

        <div class="grid gap-4 xl:grid-cols-3">
          <div class="space-y-4 xl:col-span-2">
            <SettingsBasicConfigPanel
              :settings="localSettings"
              :refresh-account-interval-field="refreshAccountIntervalField"
              :image-retention-days-field="imageRetentionDaysField"
              :log-retention-days-field="logRetentionDaysField"
              :image-poll-timeout-field="imagePollTimeoutField"
              :image-stream-timeout-field="imageStreamTimeoutField"
              :image-account-concurrency-field="imageAccountConcurrencyField"
              :image-timeout-retry-field="imageTimeoutRetryField"
              :proxy-busy="proxyBusy"
              :proxy-test-result="proxyTestResult"
              @clear-proxy-test-result="proxyTestResult = null"
              @test-default-proxy="testDefaultProxy"
            />

            <SettingsProxyRuntimePanel
              v-model:clearance-test-target="clearanceTestTarget"
              :settings="localSettings"
              :runtime-status="proxyRuntimeStatus"
              :runtime-loading="proxyRuntimeLoading"
              :runtime-testing="proxyRuntimeTesting"
              :clearance-test-result="clearanceTestResult"
              :clearance-timeout-field="clearanceTimeoutField"
              :clearance-refresh-interval-field="clearanceRefreshIntervalField"
              @clear-clearance-test-result="clearanceTestResult = null"
              @refresh-runtime-status="loadProxyRuntimeStatus(false)"
              @test-clearance="testProxyClearance"
            />

            <FormSection title="全局附加指令">
              <FormField label="全局系统提示词">
                <template #label-extra>
                  <HelpTip text="每次请求都会作为 system 消息注入。" />
                </template>
                <textarea
                  v-model="localSettings.global_system_prompt"
                  rows="5"
                  class="ui-textarea-sm"
                  placeholder="例如：先判断用户提示词是否合规；遇到违法、色情、暴力、仇恨等请求时拒绝回答。"
                ></textarea>
              </FormField>

              <FormField label="敏感词">
                <textarea
                  v-model="sensitiveWordsText"
                  rows="5"
                  class="ui-textarea-sm"
                  placeholder="一行一个，命中即拒绝"
                ></textarea>
              </FormField>
            </FormSection>
          </div>

          <SettingsBasicPolicyPanel
            :settings="localSettings"
            :image-settle-seconds-field="imageSettleSecondsField"
            @set-log-level="setLogLevel"
          />
        </div>
      </div>

      <SettingsImageErrorsPanel
        v-else-if="activeSettingsTab === 'image-errors'"
        :settings="localSettings"
      />

      <SettingsStorageReviewPanel
        v-else-if="activeSettingsTab === 'storage'"
        :settings="localSettings"
        :image-storage-busy="imageStorageBusy"
        :image-storage-test-result="imageStorageTestResult"
        @test-storage="testImageStorageConnection"
        @sync-storage="syncImageStorageFiles"
      />

      <SettingsPromptSourcesPanel
        v-else-if="activeSettingsTab === 'prompts'"
      />

      <SettingsBackupPanel
        v-else-if="activeSettingsTab === 'backup'"
        :settings="localSettings"
        :backup-interval-minutes-field="backupIntervalMinutesField"
        :backup-rotation-keep-field="backupRotationKeepField"
        :backup-busy="backupBusy"
        :backup-loading="backupLoading"
        :backup-state="backupState"
        :backup-items="backupItems"
        :backup-test-result="backupTestResult"
        :backup-status-text="backupStatusText"
        @test-connection="testBackupConnection"
        @run-now="runBackupNow"
        @load-backups="loadBackups"
        @delete-item="deleteBackupItem"
      />

      <SettingsIntegrationsPanel
        v-else-if="activeSettingsTab === 'canvas' || activeSettingsTab === 'api-docs'"
        :mode="activeSettingsTab"
        :settings="localSettings"
        :class="activeSettingsTab === 'canvas' ? 'max-w-3xl' : ''"
      />
    </PagePanel>

    <SettingsUserKeysPanel
      v-if="localSettings && activeSettingsTab === 'keys'"
      :user-keys="userKeys"
      :user-keys-loading="userKeysLoading"
      :user-key-busy="userKeyBusy"
      :new-user-key="newUserKey"
      @load="loadUserKeys"
      @create="openUserKeyCreateModal"
      @copy="copyUserKey"
      @edit="openUserKeyEditModal"
      @toggle="toggleUserKey"
      @delete="deleteUserKey"
    />

    <SettingsExternalSourcesPanel
      v-if="localSettings && (activeSettingsTab === 'cpa' || activeSettingsTab === 'sub2api')"
      :active-tab="activeSettingsTab"
      :cpa-pools="cpaPools"
      :cpa-loading="cpaLoading"
      :sub2api-servers="sub2apiServers"
      :sub2api-loading="sub2apiLoading"
      :sub2api-groups="sub2apiGroups"
      :sub2api-groups-loading-id="sub2apiGroupsLoadingId"
      :saving-external-source="savingExternalSource"
      :testing-external-source="testingExternalSource"
      :external-sources-loading="externalSourcesLoading"
      @load="loadExternalSources"
      @create-cpa="openCPAModal"
      @import-cpa="openCPAImport"
      @test-cpa="testCPAPool"
      @edit-cpa="editCPAPool"
      @delete-cpa="deleteCPAPool"
      @create-sub2api="openSub2APIModal"
      @import-sub2api="openSub2APIImport"
      @test-sub2api="testSub2APIServer"
      @load-sub2api-groups="loadSub2APIGroups"
      @edit-sub2api="editSub2APIServer"
      @delete-sub2api="deleteSub2APIServer"
    />

    <PagePanel v-if="!localSettings" class="py-10 text-center text-sm text-muted-foreground">
      <PageLoadingState
        v-if="settingsStore.isLoading"
        title="正在加载设置"
        description="读取系统配置、存储配置和外部连接。"
      />
      <StateBlock
        v-else
        title="设置加载失败"
        :description="settingsLoadError || '未获取到系统配置，请重新加载。'"
      >
        <Button size="sm" variant="outline" root-class="mt-4" @click="reloadSettings">
          重新加载
        </Button>
      </StateBlock>
    </PagePanel>

    <SettingsUserKeyModals
      :modal="userKeyModal"
      :form="userKeyForm"
      :editing-user-key="editingUserKey"
      :busy="userKeyBusy"
      @close="closeUserKeyModal"
      @create="createUserKey"
      @update="updateUserKey"
    />

    <SettingsExternalSourceModals
      :modal="externalSourceModal"
      :cpa-form="cpaForm"
      :sub2api-form="sub2apiForm"
      :editing-cpa-pool-id="editingCPAPoolId"
      :editing-sub2api-id="editingSub2APIId"
      :saving-external-source="savingExternalSource"
      @close="closeExternalSourceModal"
      @save-cpa="saveCPAPool"
      @save-sub2api="saveSub2APIServer"
    />

    <ModalShell
      :open="Boolean(remoteImportModal)"
      max-width="58rem"
      :z-index="135"
      close-on-backdrop
      @close="closeRemoteImportModal"
    >
      <ModalHeader
        :title="remoteImportModal === 'cpa' ? '从 CPA 导入账号' : '从 Sub2API 导入账号'"
        :subtitle="remoteImportModal === 'cpa' ? '读取已保存 CPA 连接中的账号文件。' : '读取已保存 Sub2API 连接中的 OpenAI 账号。'"
        :close-disabled="remoteImportBusy"
        :bordered="false"
        @close="closeRemoteImportModal"
      />
      <ModalBody>
        <RemoteAccountImportPanel
          v-if="remoteImportModal === 'cpa'"
          mode="cpa"
          :cpa-pool-id="remoteImportCPAPoolId"
          @busy-change="remoteImportBusy = $event"
          @imported="handleRemoteImportDone"
        />
        <RemoteAccountImportPanel
          v-else-if="remoteImportModal === 'sub2api'"
          mode="sub2api"
          :sub2api-server-id="remoteImportSub2APIServerId"
          :sub2api-group-id="remoteImportSub2APIGroupId"
          @busy-change="remoteImportBusy = $event"
          @imported="handleRemoteImportDone"
        />
      </ModalBody>
    </ModalShell>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'
import { Button, FormField, FormSection, HelpTip } from 'nanocat-ui'
import { normalizeProxyRuntime } from '@/api/settings'
import { usePageRuntime } from '@/composables/usePageRuntime'
import ConsoleSegmentedTabs from '@/components/ai/ConsoleSegmentedTabs.vue'
import ModalBody from '@/components/ai/ModalBody.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import PageLoadingState from '@/components/ai/PageLoadingState.vue'
import PagePanel from '@/components/ai/PagePanel.vue'
import StateBlock from '@/components/ai/StateBlock.vue'
import {
  backupStatusText as buildBackupStatusText,
  settingsTabs,
} from '@/views/settings/settingsView'
import SettingsBasicConfigPanel from '@/views/settings/SettingsBasicConfigPanel.vue'
import SettingsBasicPolicyPanel from '@/views/settings/SettingsBasicPolicyPanel.vue'
import SettingsBackupPanel from '@/views/settings/SettingsBackupPanel.vue'
import SettingsExternalSourceModals from '@/views/settings/SettingsExternalSourceModals.vue'
import SettingsExternalSourcesPanel from '@/views/settings/SettingsExternalSourcesPanel.vue'
import SettingsImageErrorsPanel from '@/views/settings/SettingsImageErrorsPanel.vue'
import SettingsIntegrationsPanel from '@/views/settings/SettingsIntegrationsPanel.vue'
import SettingsProxyRuntimePanel from '@/views/settings/SettingsProxyRuntimePanel.vue'
import SettingsPromptSourcesPanel from '@/views/settings/SettingsPromptSourcesPanel.vue'
import SettingsUserKeyModals from '@/views/settings/SettingsUserKeyModals.vue'
import SettingsStorageReviewPanel from '@/views/settings/SettingsStorageReviewPanel.vue'
import SettingsUserKeysPanel from '@/views/settings/SettingsUserKeysPanel.vue'
import { useSettingsBackupRuntime } from '@/views/settings/settingsBackupRuntime'
import { useSettingsConfigRuntime } from '@/views/settings/settingsConfigRuntime'
import { useSettingsExternalSourcesRuntime } from '@/views/settings/settingsExternalSourcesRuntime'
import { useSettingsImageStorageRuntime } from '@/views/settings/settingsImageStorageRuntime'
import { useSettingsProxyRuntime } from '@/views/settings/settingsProxyRuntime'
import { useSettingsTabRuntime } from '@/views/settings/settingsTabRuntime'
import { useSettingsUserKeysRuntime } from '@/views/settings/settingsUserKeysRuntime'
import { useNumberSettingField } from '@/views/settings/useNumberSettingField'

defineOptions({ name: 'Settings' })

const RemoteAccountImportPanel = defineAsyncComponent(() => import('@/components/ai/RemoteAccountImportPanel.vue'))

const pageRuntime = usePageRuntime('settings')

const SETTINGS_RELOAD_REQUEST_KEY = 'settings:reload'
const PROXY_RUNTIME_REQUEST_KEY = 'settings:proxy-runtime'
const USER_KEYS_REQUEST_KEY = 'settings:user-keys'
const BACKUPS_REQUEST_KEY = 'settings:backups'
const CPA_POOLS_REQUEST_KEY = 'settings:cpa-pools'
const SUB2API_SERVERS_REQUEST_KEY = 'settings:sub2api-servers'

const settingsConfigRuntime = useSettingsConfigRuntime({
  runtime: pageRuntime,
  requestKey: SETTINGS_RELOAD_REQUEST_KEY,
  afterReload: () => loadProxyRuntimeStatus(true),
  afterSave: () => loadProxyRuntimeStatus(true),
})
const settingsStore = settingsConfigRuntime.settingsStore
const localSettings = settingsConfigRuntime.localSettings
const activeSettingsTab = settingsConfigRuntime.activeSettingsTab
const isSaving = settingsConfigRuntime.isSaving
const settingsLoadError = settingsConfigRuntime.settingsLoadError
const hasUnsavedSettings = settingsConfigRuntime.hasUnsavedSettings
const requireSavedSettings = settingsConfigRuntime.requireSavedSettings
const reloadSettings = settingsConfigRuntime.reloadSettings
const handleSave = settingsConfigRuntime.handleSave
const backupRuntime = useSettingsBackupRuntime({
  runtime: pageRuntime,
  requestKey: BACKUPS_REQUEST_KEY,
  requireSavedSettings,
})
const backupsLoaded = backupRuntime.backupsLoaded
const backupBusy = backupRuntime.backupBusy
const backupLoading = backupRuntime.backupLoading
const backupState = backupRuntime.backupState
const backupItems = backupRuntime.backupItems
const backupTestResult = backupRuntime.backupTestResult
const loadBackups = backupRuntime.loadBackups
const testBackupConnection = backupRuntime.testBackupConnection
const runBackupNow = backupRuntime.runBackupNow
const deleteBackupItem = backupRuntime.deleteBackupItem
const backupStatusText = computed(() => buildBackupStatusText(backupState.value))
const externalSourcesRuntime = useSettingsExternalSourcesRuntime({
  runtime: pageRuntime,
  cpaRequestKey: CPA_POOLS_REQUEST_KEY,
  sub2apiRequestKey: SUB2API_SERVERS_REQUEST_KEY,
})
const externalSourcesLoaded = externalSourcesRuntime.externalSourcesLoaded
const cpaLoading = externalSourcesRuntime.cpaLoading
const sub2apiLoading = externalSourcesRuntime.sub2apiLoading
const savingExternalSource = externalSourcesRuntime.savingExternalSource
const testingExternalSource = externalSourcesRuntime.testingExternalSource
const externalSourceModal = externalSourcesRuntime.externalSourceModal
const remoteImportModal = externalSourcesRuntime.remoteImportModal
const remoteImportCPAPoolId = externalSourcesRuntime.remoteImportCPAPoolId
const remoteImportSub2APIServerId = externalSourcesRuntime.remoteImportSub2APIServerId
const remoteImportSub2APIGroupId = externalSourcesRuntime.remoteImportSub2APIGroupId
const remoteImportBusy = externalSourcesRuntime.remoteImportBusy
const cpaPools = externalSourcesRuntime.cpaPools
const sub2apiServers = externalSourcesRuntime.sub2apiServers
const sub2apiGroups = externalSourcesRuntime.sub2apiGroups
const sub2apiGroupsLoadingId = externalSourcesRuntime.sub2apiGroupsLoadingId
const editingCPAPoolId = externalSourcesRuntime.editingCPAPoolId
const editingSub2APIId = externalSourcesRuntime.editingSub2APIId
const cpaForm = externalSourcesRuntime.cpaForm
const sub2apiForm = externalSourcesRuntime.sub2apiForm
const externalSourcesLoading = externalSourcesRuntime.externalSourcesLoading
const openCPAModal = externalSourcesRuntime.openCPAModal
const editCPAPool = externalSourcesRuntime.editCPAPool
const saveCPAPool = externalSourcesRuntime.saveCPAPool
const deleteCPAPool = externalSourcesRuntime.deleteCPAPool
const testCPAPool = externalSourcesRuntime.testCPAPool
const openSub2APIModal = externalSourcesRuntime.openSub2APIModal
const editSub2APIServer = externalSourcesRuntime.editSub2APIServer
const saveSub2APIServer = externalSourcesRuntime.saveSub2APIServer
const deleteSub2APIServer = externalSourcesRuntime.deleteSub2APIServer
const loadSub2APIGroups = externalSourcesRuntime.loadSub2APIGroups
const testSub2APIServer = externalSourcesRuntime.testSub2APIServer
const openCPAImport = externalSourcesRuntime.openCPAImport
const openSub2APIImport = externalSourcesRuntime.openSub2APIImport
const closeRemoteImportModal = externalSourcesRuntime.closeRemoteImportModal
const closeExternalSourceModal = externalSourcesRuntime.closeExternalSourceModal
const loadExternalSources = externalSourcesRuntime.loadExternalSources
const handleRemoteImportDone = externalSourcesRuntime.handleRemoteImportDone
const userKeysRuntime = useSettingsUserKeysRuntime({
  runtime: pageRuntime,
  requestKey: USER_KEYS_REQUEST_KEY,
})
const userKeys = userKeysRuntime.userKeys
const userKeysLoaded = userKeysRuntime.userKeysLoaded
const userKeysLoading = userKeysRuntime.userKeysLoading
const userKeyBusy = userKeysRuntime.userKeyBusy
const userKeyModal = userKeysRuntime.userKeyModal
const editingUserKey = userKeysRuntime.editingUserKey
const newUserKey = userKeysRuntime.newUserKey
const userKeyForm = userKeysRuntime.userKeyForm
const copyUserKey = userKeysRuntime.copyUserKey
const openUserKeyCreateModal = userKeysRuntime.openUserKeyCreateModal
const openUserKeyEditModal = userKeysRuntime.openUserKeyEditModal
const closeUserKeyModal = userKeysRuntime.closeUserKeyModal
const loadUserKeys = userKeysRuntime.loadUserKeys
const createUserKey = userKeysRuntime.createUserKey
const updateUserKey = userKeysRuntime.updateUserKey
const toggleUserKey = userKeysRuntime.toggleUserKey
const deleteUserKey = userKeysRuntime.deleteUserKey

const sensitiveWordsText = computed({
  get: () => (localSettings.value?.sensitive_words || []).join('\n'),
  set: (value: string) => {
    if (!localSettings.value) return
    localSettings.value.sensitive_words = value
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean)
  },
})

const imageStorageRuntime = useSettingsImageStorageRuntime({ requireSavedSettings })
const imageStorageBusy = imageStorageRuntime.imageStorageBusy
const imageStorageTestResult = imageStorageRuntime.imageStorageTestResult
const testImageStorageConnection = imageStorageRuntime.testImageStorageConnection
const syncImageStorageFiles = imageStorageRuntime.syncImageStorageFiles
const proxyRuntime = useSettingsProxyRuntime({
  runtime: pageRuntime,
  requestKey: PROXY_RUNTIME_REQUEST_KEY,
  localSettings,
  requireSavedSettings,
})
const proxyBusy = proxyRuntime.proxyBusy
const proxyTestResult = proxyRuntime.proxyTestResult
const proxyRuntimeLoading = proxyRuntime.proxyRuntimeLoading
const proxyRuntimeTesting = proxyRuntime.proxyRuntimeTesting
const proxyRuntimeStatus = proxyRuntime.proxyRuntimeStatus
const clearanceTestTarget = proxyRuntime.clearanceTestTarget
const clearanceTestResult = proxyRuntime.clearanceTestResult
const testDefaultProxy = proxyRuntime.testDefaultProxy
const loadProxyRuntimeStatus = proxyRuntime.loadProxyRuntimeStatus
const testProxyClearance = proxyRuntime.testProxyClearance

const imageRetentionDaysField = useNumberSettingField(
  () => localSettings.value?.image_retention_days ?? 15,
  (value) => {
    if (!localSettings.value) return
    localSettings.value.image_retention_days = value
  },
  { integer: true, min: 1, fallback: 15 },
)
const logRetentionDaysField = useNumberSettingField(
  () => localSettings.value?.log_retention_days ?? 30,
  (value) => {
    if (!localSettings.value) return
    localSettings.value.log_retention_days = value
  },
  { integer: true, min: 1, fallback: 30 },
)
const refreshAccountIntervalField = useNumberSettingField(
  () => localSettings.value?.refresh_account_interval_minute ?? 5,
  (value) => {
    if (!localSettings.value) return
    localSettings.value.refresh_account_interval_minute = value
  },
  { integer: true, min: 1, fallback: 5 },
)
const imagePollTimeoutField = useNumberSettingField(
  () => localSettings.value?.image_poll_timeout_secs ?? 120,
  (value) => {
    if (!localSettings.value) return
    localSettings.value.image_poll_timeout_secs = value
  },
  { integer: true, min: 1, fallback: 120 },
)
const imageStreamTimeoutField = useNumberSettingField(
  () => localSettings.value?.image_stream_timeout_secs ?? 300,
  (value) => {
    if (!localSettings.value) return
    localSettings.value.image_stream_timeout_secs = value
  },
  { integer: true, min: 1, fallback: 300 },
)
const imageAccountConcurrencyField = useNumberSettingField(
  () => localSettings.value?.image_account_concurrency ?? 3,
  (value) => {
    if (!localSettings.value) return
    localSettings.value.image_account_concurrency = value
  },
  { integer: true, min: 1, fallback: 3 },
)
const imageTimeoutRetryField = useNumberSettingField(
  () => localSettings.value?.image_timeout_retry_secs ?? 30,
  (value) => {
    if (!localSettings.value) return
    localSettings.value.image_timeout_retry_secs = value
  },
  { integer: true, min: 1, fallback: 30 },
)
const imageSettleSecondsField = useNumberSettingField(
  () => localSettings.value?.image_settle_secs ?? 5,
  (value) => {
    if (!localSettings.value) return
    localSettings.value.image_settle_secs = value
  },
  { min: 0.5, fallback: 5 },
)
const clearanceTimeoutField = useNumberSettingField(
  () => localSettings.value?.proxy_runtime?.clearance.timeout_sec ?? 60,
  (value) => {
    if (!localSettings.value) return
    localSettings.value.proxy_runtime = normalizeProxyRuntime(localSettings.value.proxy_runtime)
    localSettings.value.proxy_runtime.clearance.timeout_sec = value
  },
  { integer: true, min: 1, fallback: 60 },
)
const clearanceRefreshIntervalField = useNumberSettingField(
  () => localSettings.value?.proxy_runtime?.clearance.refresh_interval ?? 3600,
  (value) => {
    if (!localSettings.value) return
    localSettings.value.proxy_runtime = normalizeProxyRuntime(localSettings.value.proxy_runtime)
    localSettings.value.proxy_runtime.clearance.refresh_interval = value
  },
  { integer: true, min: 60, fallback: 3600 },
)
const backupIntervalMinutesField = useNumberSettingField(
  () => localSettings.value?.backup?.interval_minutes ?? 1440,
  (value) => { if (localSettings.value) localSettings.value.backup.interval_minutes = value },
  { integer: true, min: 1, fallback: 1440 },
)
const backupRotationKeepField = useNumberSettingField(
  () => localSettings.value?.backup?.rotation_keep ?? 10,
  (value) => { if (localSettings.value) localSettings.value.backup.rotation_keep = value },
  { integer: true, min: 0, fallback: 10 },
)

function setLogLevel(level: string, enabled: boolean) {
  if (!localSettings.value) return
  const current = Array.isArray(localSettings.value.log_levels)
    ? localSettings.value.log_levels
    : []
  localSettings.value.log_levels = enabled
    ? Array.from(new Set([...current, level]))
    : current.filter((item) => item !== level)
}

useSettingsTabRuntime({
  runtime: pageRuntime,
  activeTab: activeSettingsTab,
  reloadSettings,
  tabLoaders: [
    {
      tabs: ['keys'],
      loaded: userKeysLoaded,
      load: loadUserKeys,
    },
    {
      tabs: ['backup'],
      loaded: backupsLoaded,
      load: loadBackups,
    },
    {
      tabs: ['cpa', 'sub2api'],
      loaded: externalSourcesLoaded,
      load: loadExternalSources,
    },
  ],
  invalidators: [
    settingsConfigRuntime.invalidate,
    proxyRuntime.invalidate,
    userKeysRuntime.invalidate,
    backupRuntime.invalidate,
    externalSourcesRuntime.invalidate,
  ],
  shouldSkipActivateReload: () => Boolean(
    hasUnsavedSettings.value ||
    isSaving.value ||
    settingsStore.isLoading ||
    imageStorageBusy.value ||
    proxyBusy.value ||
    proxyRuntimeTesting.value ||
    backupBusy.value ||
    savingExternalSource.value ||
    testingExternalSource.value ||
    userKeyBusy.value ||
    userKeyModal.value ||
    externalSourceModal.value ||
    remoteImportModal.value ||
    remoteImportBusy.value,
  ),
})
</script>
