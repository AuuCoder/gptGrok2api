<template>
  <div class="relative space-y-8">
    <div class="flex items-center justify-between gap-3">
      <ConsoleSegmentedTabs
        :model-value="activeAccountPlatform"
        :options="accountPlatformOptions"
        fit="content"
        aria-label="账号平台"
        @update:model-value="setActiveAccountPlatform(String($event))"
      />
    </div>

    <PagePanel v-if="activeAccountPlatform === 'gpt'" class="space-y-5">
      <div class="accounts-toolbar">
        <div class="accounts-toolbar-row accounts-toolbar-row-main">
          <FilterToolbar class="accounts-toolbar-filters" :bordered="false">
            <Input
              :model-value="keyword"
              type="text"
              placeholder="搜索账号 ID / 邮箱 / Token / 类型 / 来源"
              block
              root-class="min-w-[14rem] flex-1 md:max-w-sm"
              @update:model-value="keyword = $event.trim()"
            />
            <GroupedSelectMenu
              v-model="statusFilter"
              :options="statusFilterOptions"
              placeholder="状态筛选"
              selected-indicator="none"
              aria-label="账号状态筛选"
            />
            <GroupedSelectMenu
              v-model="groupFilter"
              :options="groupFilterOptions"
              placeholder="账号组"
              selected-indicator="none"
              aria-label="账号组筛选"
            />
          </FilterToolbar>

          <div class="accounts-toolbar-summary">
            <AccountSelectionSummary
              :all-selected="allVisibleSelected"
              :total-count="accountListTotal"
              :selected-count="selectedCount"
              :view-mode="viewMode"
              @toggle-all="toggleSelectAllVisible"
              @update:view-mode="setViewMode"
            />
          </div>
        </div>

        <div class="accounts-toolbar-row accounts-toolbar-row-actions">
          <div class="accounts-toolbar-action-cluster">
            <FilterToolbar class="accounts-toolbar-group accounts-toolbar-group-ops" :bordered="false" gap="tight">
              <Button
                size="sm"
                variant="outline"
                :root-class="accountToolbarButtonClass"
                :disabled="accountGroupsLoading"
                @click="openAccountGroupsModal"
              >
                账号组管理
              </Button>
              <FloatingActionMenu
                label="导入 / 添加"
                :items="accountEntryItems"
                :disabled="importBusy"
                align="left"
                :trigger-class="accountToolbarMenuClass"
                @select="handleAccountEntryAction"
              />
              <FloatingActionMenu
                label="导出"
                :items="exportMenuItems"
                :disabled="exportBusy"
                align="left"
                :trigger-class="accountToolbarMenuClass"
                @select="handleExportAction"
              />
              <FloatingActionMenu
                label="批量操作"
                :items="toolbarBatchMenuItems"
                :disabled="batchBusy"
                align="left"
                :trigger-class="accountToolbarMenuClass"
                @select="handleToolbarBatchAction"
              />
            </FilterToolbar>
          </div>

          <FilterToolbar class="accounts-toolbar-group accounts-toolbar-group-refresh" :bordered="false" gap="tight">
            <Button
              size="sm"
              variant="outline"
              :root-class="accountToolbarSecondaryClass"
              :disabled="loading || batchBusy"
              @click="loadData"
            >
              刷新列表
            </Button>
          </FilterToolbar>
        </div>
      </div>

      <PageLoadingState
        v-if="loading && filteredAccounts.length === 0"
        title="正在加载账号"
        description="读取账号列表、分组和分页状态。"
      />

      <TableShell v-else-if="viewMode === 'list'">
        <table class="min-w-[1080px] w-full text-left text-sm">
          <thead class="text-xs uppercase tracking-[0.16em] text-muted-foreground">
            <tr>
              <th class="w-12 py-3 pr-4">
                <Checkbox
                  :model-value="allVisibleSelected"
                  @update:model-value="toggleSelectAllVisible"
                />
              </th>
              <th class="py-3 pr-5">TOKEN</th>
              <th class="py-3 pr-5">类型 / 来源</th>
              <th class="py-3 pr-5">状态</th>
              <th class="py-3 pr-5">账户信息</th>
              <th class="py-3 pr-5">创建时间</th>
              <th class="py-3 pr-5">图片额度</th>
              <th class="py-3 pr-5">恢复时间</th>
              <th class="py-3 pr-5">成功 / 失败</th>
              <th class="py-3 text-right">操作</th>
            </tr>
          </thead>
          <tbody class="text-sm text-foreground">
            <tr v-if="!loading && filteredAccounts.length === 0">
              <td colspan="10" class="py-6">
                <EmptyState
                  plain
                  title="暂无账号数据"
                  description="可以先用 OAuth 登录已有账号，也可以导入 Access Token、Session JSON 或 CPA JSON 文件。"
                />
              </td>
            </tr>
            <AccountTableRow
              v-for="item in pagedAccounts"
              :key="item.id"
              :item="item"
              :selected="isSelected(item.id)"
              :refreshing="refreshingAccountId === item.id"
              :resetting="resettingAccountId === item.id"
              :status-detail-card-class="accountStatusDetailCardClass"
              :status-detail-text="accountStatusDetailText"
              @toggle-select="toggleSelect"
              @copy-token="copyAccountToken"
              @edit="openEditModal"
              @toggle-enabled="toggleEnabled"
              @refresh-token="refreshToken"
              @reset-state="resetAccountState"
              @copy-final-checkout-link="copyFinalCheckoutLink"
              @open-final-checkout-link="openFinalCheckoutLink"
              @remove="removeAccount"
            />
          </tbody>
        </table>
      </TableShell>

      <div v-else class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        <div v-if="!loading && filteredAccounts.length === 0" class="col-span-full">
          <EmptyState
            plain
            title="暂无账号数据"
            description="可以先用 OAuth 登录已有账号，也可以导入 Access Token、Session JSON 或 CPA JSON 文件。"
          />
        </div>

        <AccountGridCard
          v-for="item in pagedAccounts"
          :key="`${item.id}-card`"
          :item="item"
          :selected="isSelected(item.id)"
          :refreshing="refreshingAccountId === item.id"
          :resetting="resettingAccountId === item.id"
          :status-detail-card-class="accountStatusDetailCardClass"
          :status-detail-text="accountStatusDetailText"
          @toggle-select="toggleSelect"
          @copy-token="copyAccountToken"
          @edit="openEditModal"
          @toggle-enabled="toggleEnabled"
          @refresh-token="refreshToken"
          @reset-state="resetAccountState"
          @copy-final-checkout-link="copyFinalCheckoutLink"
          @open-final-checkout-link="openFinalCheckoutLink"
          @remove="removeAccount"
        />
      </div>

      <ListPagination
        v-model:page="currentPage"
        v-model:page-size="pageSize"
        :total-count="accountListTotal"
        :page-size-options="pageSizeOptions"
        unit="个账号"
        :disabled="loading"
      />
    </PagePanel>

    <PagePanel v-else class="space-y-5">
      <MetricStrip
        :items="grokMetricItems"
        columns-class="grid-cols-2 md:grid-cols-3 xl:grid-cols-4"
        density="compact"
      />
      <SurfaceBox
        v-if="!grokRuntimeAvailable && grokRuntimeError"
        tone="muted"
        density="compact"
        wrap
      >
        Grok 运行时数据暂不可用：{{ grokRuntimeError }}
      </SurfaceBox>

      <div class="accounts-toolbar">
        <div class="accounts-toolbar-row accounts-toolbar-row-main">
          <FilterToolbar class="accounts-toolbar-filters" :bordered="false">
            <Input
              :model-value="grokKeyword"
              type="text"
              placeholder="搜索 Grok 账号 ID / 邮箱 / 来源 / 状态"
              block
              root-class="min-w-[14rem] flex-1 md:max-w-sm"
              @update:model-value="grokKeyword = $event.trim()"
            />
            <GroupedSelectMenu
              v-model="grokStatusFilter"
              :options="grokStatusFilterOptions"
              placeholder="状态筛选"
              selected-indicator="none"
              aria-label="Grok 账号状态筛选"
            />
          </FilterToolbar>

          <div class="accounts-toolbar-summary">
            <AccountSelectionSummary
              :all-selected="grokAllVisibleSelected"
              :total-count="grokAccountListTotal"
              :selected-count="grokSelectedCount"
              :view-mode="grokViewMode"
              @toggle-all="toggleSelectAllVisibleGrokAccounts"
              @update:view-mode="setGrokViewMode"
            />
          </div>
        </div>

        <div class="accounts-toolbar-row accounts-toolbar-row-actions">
          <FilterToolbar class="accounts-toolbar-group accounts-toolbar-group-ops" :bordered="false" gap="tight">
            <Button
              size="sm"
              variant="outline"
              :root-class="accountToolbarSecondaryClass"
              :disabled="Boolean(grokOAuthRowAction.accountId)"
              @click="showGrokOAuthAccess = true"
            >
              OAuth 接入 ({{ grokOAuthTotal }})
            </Button>
            <Button
              size="sm"
              variant="outline"
              :root-class="accountToolbarSecondaryClass"
              :disabled="grokBatchChatTestProgress.busy || Boolean(grokConversationAccount) || Boolean(grokChattingAccountId) || grokAccountAllTotal === 0"
              title="逐个测试全部保存 SSO 的 Grok 账号，会消耗一次 Console 对话额度"
              @click="runGrokBatchChatTest"
            >
              {{ grokBatchChatTestProgress.busy ? '全部测试中...' : '全部对话测试' }}
            </Button>
            <FloatingActionMenu
              label="导出"
              :items="grokExportMenuItems"
              :disabled="grokBatchChatTestProgress.busy || grokExportBusy || grokAccountAllTotal === 0"
              align="left"
              :trigger-class="accountToolbarMenuClass"
              @select="handleGrokExportAction"
            />
          </FilterToolbar>

          <FilterToolbar class="accounts-toolbar-group accounts-toolbar-group-refresh" :bordered="false" gap="tight">
            <Button
              size="sm"
              variant="outline"
              :root-class="accountToolbarSecondaryClass"
              :disabled="grokLoading || grokBatchChatTestProgress.busy"
              @click="loadGrokAccounts"
            >
              刷新列表
            </Button>
          </FilterToolbar>
        </div>
      </div>

      <PageLoadingState
        v-if="grokLoading && grokAccounts.length === 0"
        title="正在加载 Grok 账号"
        description="读取注册账号、登录态和分页状态。"
      />

      <TableShell v-else-if="grokViewMode === 'list'">
        <table class="min-w-[1260px] w-full text-left text-sm">
          <thead class="text-xs uppercase tracking-[0.16em] text-muted-foreground">
            <tr>
              <th class="w-12 py-3 pr-4">
                <Checkbox
                  :model-value="grokAllVisibleSelected"
                  @update:model-value="toggleSelectAllVisibleGrokAccounts"
                />
              </th>
              <th class="py-3 pr-5">账号 / TOKEN</th>
              <th class="py-3 pr-5">类型 / OAuth</th>
              <th class="py-3 pr-5">注册状态</th>
              <th class="py-3 pr-5">运行状态</th>
              <th class="py-3 pr-5">额度 A / F / E / H / C</th>
              <th class="py-3 pr-5">成功 / 失败 / 成功率</th>
              <th class="py-3 pr-5">最近使用</th>
              <th class="py-3 text-right">操作</th>
            </tr>
          </thead>
          <tbody class="text-sm text-foreground">
            <tr v-if="!grokLoading && grokAccounts.length === 0">
              <td colspan="9" class="py-6">
                <EmptyState
                  plain
                  title="暂无 Grok 账号"
                  description="Grok 纯协议注册成功后，账号会自动进入这里。"
                />
              </td>
            </tr>
            <GrokAccountTableRow
              v-for="item in grokAccounts"
              :key="item.id"
              :item="item"
              :selected="isGrokAccountSelected(item.id)"
              :runtime-available="grokRuntimeAvailable"
              :busy="grokBatchBusy || grokBatchChatTestProgress.busy"
              :syncing="grokSyncingAccountId === item.id"
              :refreshing="grokRefreshingAccountId === item.id"
              :testing="grokTestingAccountId === item.id"
              :chatting="grokChattingAccountId === item.id"
              :toggling="grokTogglingAccountId === item.id"
              :deleting="grokRemovingAccountId === item.id"
              :oauth-action="grokOAuthActionFor(item)"
              @toggle-select="toggleGrokAccountSelection"
              @credentials="openGrokLoginCredentials"
              @sync="syncGrokAccount"
              @refresh="refreshGrokAccount"
              @test="testGrokAccount"
              @chat="openGrokConversationTest"
              @toggle-disabled="toggleGrokAccountDisabled"
              @remove="removeGrokAccount"
              @oauth-sync="syncGrokOAuthAccount"
              @oauth-refresh="refreshGrokOAuthAccount"
              @oauth-toggle="toggleGrokOAuthAccount"
              @oauth-remove="removeGrokOAuthAccount"
            />
          </tbody>
        </table>
      </TableShell>

      <div v-else class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        <div v-if="!grokLoading && grokAccounts.length === 0" class="col-span-full">
          <EmptyState
            plain
            title="暂无 Grok 账号"
            description="Grok 纯协议注册成功后，账号会自动进入这里。"
          />
        </div>

        <GrokAccountGridCard
          v-for="item in grokAccounts"
          :key="`${item.id}-grok-card`"
          :item="item"
          :selected="isGrokAccountSelected(item.id)"
          :runtime-available="grokRuntimeAvailable"
          :busy="grokBatchBusy || grokBatchChatTestProgress.busy"
          :syncing="grokSyncingAccountId === item.id"
          :refreshing="grokRefreshingAccountId === item.id"
          :testing="grokTestingAccountId === item.id"
          :chatting="grokChattingAccountId === item.id"
          :toggling="grokTogglingAccountId === item.id"
          :deleting="grokRemovingAccountId === item.id"
          :oauth-action="grokOAuthActionFor(item)"
          @toggle-select="toggleGrokAccountSelection"
          @credentials="openGrokLoginCredentials"
          @sync="syncGrokAccount"
          @refresh="refreshGrokAccount"
          @test="testGrokAccount"
          @chat="openGrokConversationTest"
          @toggle-disabled="toggleGrokAccountDisabled"
          @remove="removeGrokAccount"
          @oauth-sync="syncGrokOAuthAccount"
          @oauth-refresh="refreshGrokOAuthAccount"
          @oauth-toggle="toggleGrokOAuthAccount"
          @oauth-remove="removeGrokOAuthAccount"
        />
      </div>

      <ListPagination
        v-model:page="grokCurrentPage"
        v-model:page-size="grokPageSize"
        :total-count="grokAccountListTotal"
        :page-size-options="grokPageSizeOptions"
        unit="个 Grok 账号"
        :disabled="grokLoading || grokBatchChatTestProgress.busy"
      />
    </PagePanel>

    <AccountBulkBar
      v-if="activeAccountPlatform === 'gpt'"
      :selected-count="selectedCount"
      :busy="batchBusy"
      :busy-label="batchActionLabel"
      :items="batchMenuItems"
      @select="handleBatchAction"
      @clear="clearSelection"
    />

    <AccountBulkBar
      v-if="activeAccountPlatform === 'grok'"
      :selected-count="grokSelectedCount"
      :busy="grokBatchBusy || grokBatchChatTestProgress.busy"
      :busy-label="grokBatchActionLabel"
      :items="grokBatchMenuItems"
      @select="runGrokBulkAction"
      @clear="clearGrokSelection"
    />

    <GrokLoginCredentialsModal
      :open="Boolean(grokCredentialsAccount)"
      :account="grokCredentialsAccount"
      @close="grokCredentialsAccount = null"
    />

    <GrokAccountConversationTestModal
      :open="Boolean(grokConversationAccount)"
      :account="grokConversationAccount"
      @close="closeGrokConversationTest"
      @running="setGrokConversationRunning"
    />

    <GrokOAuthAccountsPanel
      ref="grokOAuthPanelRef"
      :open="activeAccountPlatform === 'grok' && showGrokOAuthAccess"
      @close="showGrokOAuthAccess = false"
      @changed="handleGrokOAuthChanged"
    />

    <OperationProgressModal
      :open="grokBatchChatTestProgress.open"
      :title="grokBatchChatTestProgress.title"
      :subtitle="grokBatchChatTestProgress.subtitle"
      :total="grokBatchChatTestProgress.total"
      :current="grokBatchChatTestProgress.current"
      :status-label="grokBatchChatTestProgress.statusLabel"
      :message="grokBatchChatTestProgress.message"
      :error="grokBatchChatTestProgress.error"
      :busy="grokBatchChatTestProgress.busy"
      :can-cancel="grokBatchChatTestCanCancel"
      max-width="42rem"
      @close="grokBatchChatTestProgress.open = false"
      @cancel="cancelGrokBatchChatTest"
    >
      <template v-if="grokBatchChatTestFailureDetails.length" #details>
        <div class="space-y-2">
          <p class="ui-field-label">非成功账号（封禁与登录失效优先，最多 20 项）</p>
          <ul class="max-h-56 divide-y divide-border overflow-auto">
            <li v-for="item in grokBatchChatTestFailureDetails" :key="item.id" class="py-2 first:pt-0">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <span class="break-all font-mono text-xs text-foreground">{{ item.id }}</span>
                <span class="text-xs font-medium" :class="grokBatchChatTestResultStatusClass(item.status)">{{ grokBatchChatTestResultStatusText(item.status) }}</span>
              </div>
              <p class="mt-1 break-words text-xs leading-5 text-muted-foreground">
                {{ item.error || '未返回错误说明' }}
              </p>
            </li>
          </ul>
        </div>
      </template>
    </OperationProgressModal>

    <ModalShell :open="activeAccountPlatform === 'gpt' && showModal" max-width="44rem" :z-index="120">
            <ModalHeader :title="editingId ? '编辑账号' : '添加账号'" :bordered="false" compact @close="closeModal" />

            <ModalBody density="compact" class="space-y-3">
                <FormSection title="基础信息" surface="plain">
                  <div class="grid grid-cols-1 gap-2.5 md:grid-cols-4">
                    <label v-if="editingId" class="text-xs md:col-span-2">
                      <span class="ui-field-label">账号 ID</span>
                      <Input :model-value="form.id" disabled block />
                    </label>
                    <label class="text-xs">
                      <span class="ui-field-label">类型</span>
                      <Input
                        :model-value="form.type"
                        block
                        placeholder="free / Plus / Pro"
                        @update:model-value="form.type = $event.trim()"
                      />
                    </label>
                    <div class="text-xs">
                      <span class="ui-field-label">状态</span>
                      <GroupedSelectMenu
                        v-model="form.status"
                        :options="accountStatusOptions"
                        placeholder="状态"
                        selected-indicator="none"
                        aria-label="账号状态"
                        block
                      />
                    </div>
                  </div>
                </FormSection>

                <FormSection surface="plain">
                  <label class="block text-xs">
                    <span class="ui-field-label">Access Token（必填）</span>
                    <textarea
                      v-model.trim="form.access_token"
                      rows="3"
                      class="ui-textarea-sm font-mono"
                      placeholder="粘贴完整 access token"
                      :disabled="!!editingId"
                    ></textarea>
                  </label>
                </FormSection>

                <FormSection title="调度属性" surface="plain">
                  <div class="grid grid-cols-1 gap-2 md:grid-cols-3">
                    <label class="text-xs">
                      <span class="ui-field-label">来源</span>
                      <Input
                        :model-value="form.source_type"
                        block
                        placeholder="web / oauth_login / codex"
                        @update:model-value="form.source_type = $event.trim()"
                      />
                    </label>
                    <label class="text-xs">
                      <span class="ui-field-label">图片额度</span>
                      <Input
                        :model-value="form.quota"
                        type="number"
                        block
                        placeholder="留空表示未知"
                        @update:model-value="form.quota = $event.trim()"
                      />
                    </label>
                    <label class="text-xs">
                      <span class="ui-field-label">账号组</span>
                      <GroupedSelectMenu
                        v-model="form.group_id"
                        :options="accountGroupOptions"
                        :disabled="accountGroupsLoading"
                        aria-label="账号组"
                        selected-indicator="none"
                        block
                      />
                    </label>
                    <div class="space-y-2 text-xs md:col-span-3">
                      <div class="grid grid-cols-1 gap-2 md:grid-cols-[11rem_minmax(0,1fr)]">
                        <label>
                          <span class="ui-field-label">代理模式</span>
                          <GroupedSelectMenu
                            :model-value="proxyMode"
                            :options="accountProxyModeOptions"
                            aria-label="代理模式"
                            selected-indicator="none"
                            block
                            @update:model-value="setProxyMode"
                          />
                        </label>

                        <label v-if="proxyMode === 'group'">
                          <span class="ui-field-label">代理组（多节点）</span>
                          <GroupedSelectMenu
                            :model-value="selectedProxyGroupId"
                            :options="proxyGroupOptions"
                            :disabled="accountGroupsLoading"
                            aria-label="代理组"
                            selected-indicator="none"
                            block
                            @update:model-value="selectProxyGroup"
                          />
                        </label>

                        <label v-else-if="proxyMode === 'custom'">
                          <span class="ui-field-label">自定义代理</span>
                          <Input
                            :model-value="customProxyInput"
                            block
                            root-class="font-mono"
                            placeholder="http://127.0.0.1:7890"
                            @update:model-value="setCustomProxyInput"
                          />
                        </label>

                        <SurfaceBox v-else tone="muted" dashed density="compact" class="flex min-h-[3.25rem] items-center">
                          {{ proxyMode === 'direct' ? '该账号强制直连，不读取账号组或默认出口。' : '该账号不单独指定代理，会按账号组代理、默认出口顺序回退。' }}
                        </SurfaceBox>
                      </div>

                      <SurfaceBox tone="muted" density="compact" class="flex flex-wrap items-center justify-between gap-2">
                        <div class="min-w-0">
                          <span class="ui-field-label">当前代理</span>
                          <p class="mt-1 max-w-full truncate text-xs text-foreground" :title="accountProxyPreview">{{ accountProxyPreview }}</p>
                        </div>
                        <div class="flex flex-wrap items-center gap-2">
                          <Button
                            v-if="proxyMode === 'group'"
                            size="xs"
                            variant="outline"
                            root-class="min-w-24 justify-center"
                            :disabled="accountGroupsLoading"
                            @click="loadAccountGroups()"
                          >
                            {{ accountGroupsLoading ? '刷新中...' : '刷新代理组' }}
                          </Button>
                          <Button
                            v-if="proxyMode !== 'direct'"
                            size="xs"
                            variant="outline"
                            root-class="min-w-24 justify-center"
                            :disabled="proxyTesting || accountGroupsLoading"
                            @click="testAccountProxy"
                          >
                            {{ proxyTesting ? '测试中...' : '测试当前代理' }}
                          </Button>
                          <span v-else class="text-[11px] text-muted-foreground">直连模式无需测试出口</span>
                        </div>
                      </SurfaceBox>
                    </div>
                  </div>
                </FormSection>
            </ModalBody>

            <ModalFooter :bordered="false">
              <Button size="xs" variant="primary" root-class="min-w-14 justify-center" :disabled="saving" @click="saveAccount">
                {{ saving ? '保存中...' : '保存' }}
              </Button>
            </ModalFooter>
    </ModalShell>

    <ModalShell :open="activeAccountPlatform === 'gpt' && showAccountGroupsModal" max-width="58rem" :z-index="130">
            <ModalHeader
              title="账号组管理"
              subtitle="先创建账号组，再在账号列表勾选账号批量绑定。"
              :close-disabled="accountGroupSaving"
              compact
              @close="closeAccountGroupsModal"
            />

            <div class="grid grid-cols-1 gap-0 md:grid-cols-[18rem_1fr]">
              <div class="border-b border-border bg-muted/20 p-4 md:border-b-0 md:border-r">
                <div class="space-y-3">
                  <p class="text-sm font-medium text-foreground">
                    {{ editingAccountGroupId ? '编辑账号组' : '新建账号组' }}
                  </p>

                  <label class="block text-xs">
                    <span class="ui-field-label">账号组名称</span>
                    <Input
                      :model-value="accountGroupForm.name"
                      block
                      placeholder="高额度账号 / Codex / 手动 Token"
                      @update:model-value="accountGroupForm.name = $event.trim()"
                    />
                  </label>

                  <div class="space-y-2 text-xs">
                    <label class="block">
                      <span class="ui-field-label">默认出口</span>
                      <GroupedSelectMenu
                        :model-value="accountGroupProxyMode"
                        :options="accountProxyModeOptions"
                        aria-label="账号组默认代理模式"
                        selected-indicator="none"
                        block
                        @update:model-value="setAccountGroupProxyMode"
                      />
                    </label>

                    <label v-if="accountGroupProxyMode === 'group'" class="block">
                      <span class="ui-field-label">代理组（多节点）</span>
                      <GroupedSelectMenu
                        :model-value="selectedAccountGroupProxyGroupId"
                        :options="accountGroupProxyOptions"
                        :disabled="accountGroupsLoading"
                        aria-label="账号组默认代理组"
                        selected-indicator="none"
                        block
                        @update:model-value="selectAccountGroupProxyGroup"
                      />
                    </label>

                    <label v-else-if="accountGroupProxyMode === 'custom'" class="block">
                      <span class="ui-field-label">自定义代理</span>
                      <Input
                        :model-value="accountGroupCustomProxyInput"
                        block
                        root-class="font-mono"
                        placeholder="http://127.0.0.1:7890"
                        @update:model-value="setAccountGroupCustomProxyInput"
                      />
                    </label>

                    <SurfaceBox v-else tone="muted" dashed density="compact" class="min-h-[2.75rem]">
                      {{ accountGroupProxyMode === 'direct' ? '该账号组强制直连，组内账号不会回退默认出口。' : '账号组不单独指定代理，组内账号会继续回退默认出口。' }}
                    </SurfaceBox>

                    <SurfaceBox tone="muted" density="compact">
                      <span class="ui-field-label">当前代理</span>
                      <p class="mt-1 truncate text-xs text-foreground" :title="accountGroupProxyPreview">{{ accountGroupProxyPreview }}</p>
                    </SurfaceBox>
                  </div>

                  <SurfaceBox tag="label" density="compact" class="flex items-center gap-2">
                    <Checkbox
                      :model-value="accountGroupForm.enabled"
                      @update:model-value="accountGroupForm.enabled = Boolean($event)"
                    />
                    启用账号组
                  </SurfaceBox>

                  <label class="block text-xs">
                    <span class="ui-field-label">备注</span>
                    <textarea
                      v-model.trim="accountGroupForm.notes"
                      rows="3"
                      class="ui-textarea-sm"
                      placeholder="例如：高额度账号默认走香港代理池"
                    ></textarea>
                  </label>

                  <Button size="sm" variant="primary" root-class="w-full justify-center" :disabled="accountGroupSaving" @click="saveAccountGroup">
                    {{ accountGroupSaving ? '保存中...' : editingAccountGroupId ? '保存账号组' : '创建账号组' }}
                  </Button>
                </div>
              </div>

              <div class="max-h-[32rem] overflow-y-auto p-4">
                <StateBlock v-if="accountGroupRows.length === 0" dashed>
                  还没有账号组。先在左侧创建，比如高额度账号、Codex、手动 Token。
                </StateBlock>

                <div v-else class="space-y-2">
                  <InfoCard
                    v-for="group in accountGroupRows"
                    :key="group.id"
                    tag="article"
                    density="compact"
                  >
                    <div class="flex flex-wrap items-start justify-between gap-3">
                      <div class="min-w-0">
                        <div class="flex flex-wrap items-center gap-2">
                          <p class="font-medium text-foreground">{{ group.name }}</p>
                          <StateBadge :tone="group.enabled ? 'success' : 'muted'" size="xs">
                            {{ group.enabled ? '启用' : '停用' }}
                          </StateBadge>
                        </div>
                        <p class="mt-1 text-xs text-muted-foreground">
                          {{ group.account_count }} 个账号 · 默认出口：{{ group.proxy_label }}
                        </p>
                        <p v-if="group.notes" class="mt-1 line-clamp-2 text-xs text-muted-foreground">{{ group.notes }}</p>
                      </div>

                      <div class="flex shrink-0 items-center gap-2">
                        <Button size="xs" variant="outline" :disabled="accountGroupSaving" @click="editAccountGroup(group.raw)">
                          编辑
                        </Button>
                        <Button size="xs" variant="outline" root-class="text-rose-600" :disabled="accountGroupSaving" @click="deleteAccountGroup(group.raw)">
                          删除
                        </Button>
                      </div>
                    </div>
                  </InfoCard>
                </div>
              </div>
            </div>
    </ModalShell>

    <ModalShell :open="activeAccountPlatform === 'gpt' && showImportModal" max-width="58rem" :z-index="120">
            <ModalHeader title="导入账号" :close-disabled="importModalBusy" compact @close="closeImportModal" />

            <div class="grid grid-cols-1 gap-0 md:grid-cols-[15rem_1fr]">
              <div class="border-b border-border bg-muted/20 p-3 md:border-b-0 md:border-r">
                <div class="space-y-1">
                  <button
                    v-for="option in importModeOptions"
                    :key="option.value"
                    type="button"
                    class="w-full rounded-xl px-3 py-2 text-left text-sm transition-colors"
                    :class="importMode === option.value ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:bg-card hover:text-foreground'"
                    :disabled="importModalBusy"
                    @click="setImportMode(option.value)"
                  >
                    {{ option.label }}
                  </button>
                </div>
              </div>

              <div class="min-h-[26rem] p-4">
                <div v-if="importMode === 'oauth_login'" class="space-y-3">
                  <ImportModePanel
                    title="OAuth 登录已有账号（带自动刷新）"
                    description="用浏览器登录自己的 ChatGPT 账号，回填 callback URL 后导入 refresh_token。"
                  />
                  <div class="grid grid-cols-1 gap-3">
                    <label class="block text-xs">
                      <span class="ui-field-label">账号邮箱（可选）</span>
                      <Input
                        :model-value="oauthEmailHint"
                        type="email"
                        block
                        placeholder="name@example.com"
                        :disabled="importBusy"
                        @update:model-value="oauthEmailHint = $event.trim()"
                      />
                    </label>

                    <div class="flex flex-wrap gap-2">
                      <Button size="xs" variant="primary" :disabled="importBusy" @click="startOAuthLogin">
                        {{ oauthAuthorizeUrl ? '重新生成授权链接' : '生成并打开授权页面' }}
                      </Button>
                      <Button v-if="oauthAuthorizeUrl" size="xs" variant="outline" :disabled="importBusy" @click="openOAuthAuthorizeUrl">
                        打开授权页面
                      </Button>
                      <Button v-if="oauthAuthorizeUrl" size="xs" variant="outline" :disabled="importBusy" @click="copyOAuthAuthorizeUrl">
                        复制授权链接
                      </Button>
                    </div>

                    <SurfaceBox v-if="oauthAuthorizeUrl" tone="muted" density="compact" wrap>
                      授权链接已生成。登录完成后，把浏览器最终跳转到的 callback URL 粘贴到下方。
                      <span v-if="oauthRedirectUriPrefix">目标地址：{{ oauthRedirectUriPrefix }}</span>
                    </SurfaceBox>

                    <label class="block text-xs">
                      <span class="ui-field-label">Callback URL / Code</span>
                      <textarea
                        v-model.trim="oauthCallbackText"
                        rows="5"
                        class="ui-textarea-sm font-mono"
                        placeholder="粘贴完整 callback URL，或只粘贴 code"
                        :disabled="importBusy || !oauthSessionId"
                      ></textarea>
                    </label>

                    <div class="flex justify-end">
                      <Button
                        size="xs"
                        variant="primary"
                        :disabled="importBusy || !oauthSessionId || !oauthCallbackText.trim()"
                        @click="finishOAuthLogin"
                      >
                        {{ importBusy ? '导入中...' : '完成导入' }}
                      </Button>
                    </div>
                  </div>
                </div>

                <div v-else-if="importMode === 'access_token'" class="space-y-3">
                  <ImportModePanel
                    title="导入 Access Token"
                    description="支持直接粘贴，一行一个；也支持从 TXT 文件读取，一行一个。"
                  />
                  <textarea
                    v-model.trim="manualTokenText"
                    rows="10"
                    class="ui-textarea-sm font-mono"
                    placeholder="一行一个 access token"
                  ></textarea>
                  <div class="flex flex-wrap justify-end gap-2">
                    <Button size="xs" variant="outline" :disabled="importBusy" @click="openManualTokenFile">
                      读取 TXT 文件
                    </Button>
                    <Button size="xs" variant="primary" :disabled="importBusy || !manualTokenText.trim()" @click="importManualTokenText">
                      {{ importBusy ? '导入中...' : '开始导入' }}
                    </Button>
                  </div>
                </div>

                <div v-else-if="importMode === 'session_json'" class="space-y-3">
                  <ImportModePanel
                    title="导入 Session JSON"
                    description="从 chatgpt.com 的 session 接口复制完整 JSON，自动提取 accessToken。"
                  />
                  <textarea v-model.trim="sessionJsonText" rows="12" class="ui-textarea-sm font-mono" placeholder="粘贴完整 session JSON"></textarea>
                  <div class="flex justify-end">
                    <Button size="xs" variant="primary" :disabled="importBusy || !sessionJsonText.trim()" @click="importSessionJson">
                      {{ importBusy ? '导入中...' : '开始导入' }}
                    </Button>
                  </div>
                </div>

                <div v-else-if="importMode === 'cpa_json'" class="space-y-3">
                  <ImportModePanel
                    title="导入 CPA JSON 文件"
                    description="支持一次多选多个本地 JSON 文件，逐个读取对象里的 access_token 后导入。"
                  />
                  <StateBlock dashed compact>
                    <Button size="sm" variant="outline" :disabled="importBusy" @click="openCPAFileDialog">
                      选择 CPA JSON 文件
                    </Button>
                  </StateBlock>
                </div>

                <div v-else-if="importMode === 'remote_cpa'" class="space-y-3">
                  <RemoteAccountImportPanel
                    mode="cpa"
                    @busy-change="remoteImportBusy = $event"
                    @imported="handleRemoteImportDone"
                  />
                </div>

                <div v-else-if="importMode === 'sub2api'" class="space-y-3">
                  <RemoteAccountImportPanel
                    mode="sub2api"
                    @busy-change="remoteImportBusy = $event"
                    @imported="handleRemoteImportDone"
                  />
                </div>
              </div>
            </div>
    </ModalShell>

    <ModalShell :open="activeAccountPlatform === 'gpt' && showRefreshProgress" max-width="34rem" :z-index="140">
          <ModalHeader
            :title="refreshProgressTitle || '刷新账号信息和额度'"
            :close-disabled="batchBusy && !refreshProgress?.done"
            compact
            @close="closeRefreshProgress"
          >
            <template #actions>
              <Button
                v-if="canStopRefreshProgress"
                size="xs"
                variant="outline"
                root-class="min-w-14 justify-center text-amber-600"
                :disabled="bulkStopRequested"
                @click="requestStopRefreshProgress"
              >
                {{ bulkStopRequested ? '停止中...' : '停止' }}
              </Button>
            </template>
          </ModalHeader>
          <div class="space-y-4 px-5 py-4">
            <div class="flex items-center justify-between text-xs text-muted-foreground">
              <span>{{ refreshProgress?.processed || 0 }} / {{ refreshProgress?.total || 0 }}</span>
              <span>{{ refreshProgressPercent }}%</span>
            </div>
            <ProgressBar :value="refreshProgressPercent" aria-label="账号刷新进度" />
            <MetricStrip :items="refreshProgressItems" columns-class="grid-cols-2" density="compact" />
            <SurfaceBox v-if="refreshProgress?.error" tag="p" tone="danger" density="compact">
              {{ refreshProgress.error }}
            </SurfaceBox>
          </div>
    </ModalShell>

    <input ref="manualTokenFileInputRef" type="file" accept=".txt,text/plain" class="hidden" @change="handleManualTokenFileChange" />
    <input ref="cpaFileInputRef" type="file" accept=".json,application/json" multiple class="hidden" @change="handleCPAFileChange" />
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, reactive, ref } from 'vue'
import { Button, Checkbox, EmptyState, Input } from 'nanocat-ui'
import AccountBulkBar from '@/components/ai/AccountBulkBar.vue'
import AccountSelectionSummary from '@/components/ai/AccountSelectionSummary.vue'
import ConsoleSegmentedTabs from '@/components/ai/ConsoleSegmentedTabs.vue'
import FilterToolbar from '@/components/ai/FilterToolbar.vue'
import FloatingActionMenu from '@/components/ai/FloatingActionMenu.vue'
import FormSection from '@/components/ai/FormSection.vue'
import ImportModePanel from '@/components/ai/ImportModePanel.vue'
import InfoCard from '@/components/ai/InfoCard.vue'
import ListPagination from '@/components/ai/ListPagination.vue'
import MetricStrip from '@/components/ai/MetricStrip.vue'
import ModalBody from '@/components/ai/ModalBody.vue'
import ModalFooter from '@/components/ai/ModalFooter.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import PageLoadingState from '@/components/ai/PageLoadingState.vue'
import PagePanel from '@/components/ai/PagePanel.vue'
import ProgressBar from '@/components/ai/ProgressBar.vue'
import StateBadge from '@/components/ai/StateBadge.vue'
import StateBlock from '@/components/ai/StateBlock.vue'
import SurfaceBox from '@/components/ai/SurfaceBox.vue'
import TableShell from '@/components/ai/TableShell.vue'
import GroupedSelectMenu from '@/components/ui/GroupedSelectMenu.vue'
import type { Account } from '@/api/accounts'
import type { GrokOAuthAccount } from '@/api/grokOAuthAccounts'
import {
  grokAccountsApi,
  type GrokAccount,
  type GrokAccountsBatchChatTestJob,
  type GrokAccountsBatchChatTestResult,
  type GrokAccountsBatchChatTestSummary,
  type GrokQuotaMode,
} from '@/api/grokAccounts'
import AccountGridCard from './accounts/AccountGridCard.vue'
import AccountTableRow from './accounts/AccountTableRow.vue'
import GrokAccountConversationTestModal from './accounts/GrokAccountConversationTestModal.vue'
import GrokAccountGridCard from './accounts/GrokAccountGridCard.vue'
import GrokAccountTableRow from './accounts/GrokAccountTableRow.vue'
import GrokLoginCredentialsModal from './accounts/GrokLoginCredentialsModal.vue'
import GrokOAuthAccountsPanel from './accounts/GrokOAuthAccountsPanel.vue'
import { useAccountsPage } from './accounts/useAccountsPage'
import { useAccountActionMenuRuntime } from './accounts/accountActionMenuRuntime'
import { useGrokAccountsPage } from './accounts/useGrokAccountsPage'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'
import {
  accountGroupLabel as buildAccountGroupLabel,
  accountGroupNameMap as buildAccountGroupNameMap,
  accountProxyText,
  accountStatusDetailText as buildAccountStatusDetailText,
  buildAccountGroupRows,
  buildAccountProgressMetricItems,
} from './accounts/viewUtils'

defineOptions({ name: 'Accounts' })

const RemoteAccountImportPanel = defineAsyncComponent(() => import('@/components/ai/RemoteAccountImportPanel.vue'))
const OperationProgressModal = defineAsyncComponent(() => import('@/components/ai/OperationProgressModal.vue'))
const GROK_BATCH_CHAT_TEST_MODEL = 'grok-4.3-console'
const GROK_BATCH_CHAT_TEST_PROMPT = '你好，请只回复 OK。'
const GROK_BATCH_CHAT_TEST_POLL_INTERVAL_MS = 1200
const confirmDialog = useConfirmDialog()
const toast = useToast()

const {
  loading,
  saving,
  showModal,
  keyword,
  statusFilter,
  groupFilter,
  statusFilterOptions,
  groupFilterOptions,
  editingId,
  accounts,
  accountListTotal,
  accountAllTotal,
  selectedCount,
  allVisibleSelected,
  currentPage,
  pageSize,
  pageSizeOptions,
  batchBusy,
  batchActionLabel,
  viewMode,
  refreshingAccountId,
  resettingAccountId,
  importBusy,
  exportBusy,
  showImportModal,
  importMode,
  importModeOptions,
  oauthEmailHint,
  oauthCallbackText,
  oauthSessionId,
  oauthAuthorizeUrl,
  oauthRedirectUriPrefix,
  manualTokenText,
  sessionJsonText,
  accountGroups,
  proxyGroups,
  accountGroupsLoading,
  showAccountGroupsModal,
  accountGroupSaving,
  editingAccountGroupId,
  accountGroupForm,
  accountGroupOptions,
  accountGroupProxyOptions,
  bindAccountGroupOptions,
  selectedBindGroupId,
  proxyTesting,
  proxyMode,
  accountGroupProxyMode,
  accountProxyModeOptions,
  proxyGroupOptions,
  selectedProxyGroupId,
  customProxyInput,
  selectedAccountGroupProxyGroupId,
  accountGroupCustomProxyInput,
  accountProxyPreview,
  accountGroupProxyPreview,
  showRefreshProgress,
  refreshProgressTitle,
  refreshProgress,
  refreshProgressPercent,
  refreshProgressMetricLabel,
  refreshProgressMetricValue,
  refreshProgressStatusText,
  canStopRefreshProgress,
  bulkStopRequested,
  accountStatusOptions,
  form,
  filteredAccounts,
  pagedAccounts,
  loadData,
  loadAccountGroups,
  setViewMode,
  isSelected,
  toggleSelect,
  clearSelection,
  toggleSelectAllVisible,
  setImportMode,
  openImportModal,
  closeImportModal,
  testAccountProxy,
  openAccountGroupsModal,
  closeAccountGroupsModal,
  resetAccountGroupForm,
  editAccountGroup,
  saveAccountGroup,
  deleteAccountGroup,
  setProxyMode,
  selectProxyGroup,
  setCustomProxyInput,
  setAccountGroupProxyMode,
  selectAccountGroupProxyGroup,
  setAccountGroupCustomProxyInput,
  importManualTokenText,
  importTokenTextFile,
  importSessionJson,
  startOAuthLogin,
  openOAuthAuthorizeUrl,
  copyOAuthAuthorizeUrl,
  finishOAuthLogin,
  importLocalCPAFiles,
  refreshAllAccounts,
  requestStopRefreshProgress,
  closeRefreshProgress,
  copyAccountToken,
  extractSelectedCheckout,
  copyFinalCheckoutLink,
  openFinalCheckoutLink,
  openCreateModal,
  openEditModal,
  closeModal,
  saveAccount,
  toggleEnabled,
  refreshToken,
  resetAccountState,
  removeAccount,
  runBulkAction,
  bindSelectedAccountsToGroup,
  exportAccounts,
} = useAccountsPage()

const {
  loading: grokLoading,
  keyword: grokKeyword,
  statusFilter: grokStatusFilter,
  statusFilterOptions: grokStatusFilterOptions,
  accounts: grokAccounts,
  summary: grokSummary,
  runtimeAvailable: grokRuntimeAvailable,
  runtimeError: grokRuntimeError,
  accountListTotal: grokAccountListTotal,
  accountAllTotal: grokAccountAllTotal,
  currentPage: grokCurrentPage,
  pageSize: grokPageSize,
  pageSizeOptions: grokPageSizeOptions,
  viewMode: grokViewMode,
  selectedCount: grokSelectedCount,
  allVisibleSelected: grokAllVisibleSelected,
  isSelected: isGrokAccountSelected,
  toggleSelect: toggleGrokAccountSelection,
  toggleSelectAllVisible: toggleSelectAllVisibleGrokAccounts,
  clearSelection: clearGrokSelection,
  batchBusy: grokBatchBusy,
  batchActionLabel: grokBatchActionLabel,
  syncingAccountId: grokSyncingAccountId,
  syncAccounts: syncGrokAccounts,
  refreshingAccountId: grokRefreshingAccountId,
  refreshRuntime: refreshGrokRuntime,
  testingAccountId: grokTestingAccountId,
  testAccountValidity: testGrokAccountValidity,
  togglingAccountId: grokTogglingAccountId,
  setRuntimeDisabled: setGrokRuntimeDisabled,
  removingAccountId: grokRemovingAccountId,
  exportBusy: grokExportBusy,
  setViewMode: setGrokViewMode,
  loadData: loadGrokAccounts,
  removeAccount: removeGrokAccount,
  runBulkAction: runGrokBulkAction,
  exportAccounts: exportGrokAccounts,
} = useGrokAccountsPage()

type AccountPlatformView = 'gpt' | 'grok'

const activeAccountPlatform = ref<AccountPlatformView>('gpt')
const grokCredentialsAccount = ref<GrokAccount | null>(null)
const grokConversationAccount = ref<GrokAccount | null>(null)
const grokChattingAccountId = ref('')
type GrokOAuthPanelRef = {
  refreshAccount: (id: string) => Promise<void>
  syncModels: (id: string) => Promise<void>
  setDisabled: (account: GrokOAuthAccount) => Promise<void>
  removeAccount: (id: string) => Promise<void>
}
type GrokOAuthRowAction = 'sync' | 'refresh' | 'toggle' | 'remove'
const grokOAuthPanelRef = ref<GrokOAuthPanelRef | null>(null)
const showGrokOAuthAccess = ref(false)
const grokOAuthRowAction = reactive<{ accountId: string; action: GrokOAuthRowAction | '' }>({
  accountId: '',
  action: '',
})
const grokBatchChatTestProgress = reactive({
  open: false,
  title: '全部 Grok 对话测试',
  subtitle: `固定模型：${GROK_BATCH_CHAT_TEST_MODEL}`,
  total: 0,
  current: 0,
  statusLabel: '已提交',
  message: '',
  error: '',
  busy: false,
  jobId: '',
  jobStatus: '',
  currentId: '',
  cancelRequested: false,
  results: [] as GrokAccountsBatchChatTestResult[],
})
let grokBatchChatTestPollTimer: number | null = null
let grokBatchChatTestPollVersion = 0
let grokBatchChatTestFinalizedJobId = ''
const accountPlatformOptions = computed(() => [
  { value: 'gpt', label: `GPT (${accountAllTotal.value})` },
  { value: 'grok', label: `Grok (${grokAccountAllTotal.value})` },
])

const grokExportMenuItems = [
  { key: 'json', label: '导出 JSON' },
  { key: 'txt', label: '导出 TXT' },
]

const grokBatchMenuItems = computed(() => [
  { key: 'sync', label: '加入运行池', disabled: !grokRuntimeAvailable.value },
  { key: 'refresh', label: '刷新状态和额度', disabled: !grokRuntimeAvailable.value },
  { key: 'disable', label: '禁用选中', disabled: !grokRuntimeAvailable.value },
  { key: 'enable', label: '恢复选中', disabled: !grokRuntimeAvailable.value },
  { key: 'delete', label: '删除选中', danger: true },
])

const grokQuotaMetricLabels: Record<GrokQuotaMode, string> = {
  auto: 'Auto 余额',
  fast: 'Fast 余额',
  expert: 'Expert 余额',
  heavy: 'Heavy 余额',
  console: 'Console 余额',
}

function safeMetricNumber(value: unknown): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : 0
}

function optionalMetricNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : null
}

const grokMetricItems = computed(() => {
  const summary = grokSummary.value || {}
  const syncedCount = optionalMetricNumber(summary.synced)
  const items: Array<{
    key: string
    label: string
    value: string | number
    meta?: string
    valueClass?: string
  }> = [{
    key: 'registration-total',
    label: '注册总数',
    value: safeMetricNumber(summary.total ?? grokAccountAllTotal.value),
    meta: grokRuntimeAvailable.value
      ? (syncedCount === null ? '运行时已就绪' : `已加入 ${syncedCount}`)
      : '运行状态未连接',
  }]

  const runtimeTotal = safeMetricNumber(summary.runtime_total)
  const oauthTotal = safeMetricNumber(summary.oauth_total)
  if (oauthTotal > 0) {
    items.push({
      key: 'oauth-total',
      label: 'OAuth',
      value: oauthTotal,
      meta: `已关联 ${safeMetricNumber(summary.oauth_linked)} 个注册账号`,
      valueClass: 'text-emerald-600',
    })
  }
  if (!grokRuntimeAvailable.value || runtimeTotal <= 0) {
    items.push({
      key: 'runtime-connection',
      label: 'Grok 运行时',
      value: grokRuntimeAvailable.value ? '已连接' : '不可用',
      meta: grokRuntimeAvailable.value ? '尚无已加入运行账号' : (grokRuntimeError.value || '请检查运行时状态'),
      valueClass: grokRuntimeAvailable.value ? 'text-emerald-600' : 'text-amber-600',
    })
    return items
  }

  const runtimeStatus = summary.runtime_status || {}
  const runtimeMetrics = [
    { key: 'runtime-active', label: '正常', value: optionalMetricNumber(runtimeStatus.active), valueClass: 'text-emerald-600' },
    { key: 'runtime-cooling', label: '限流', value: optionalMetricNumber(runtimeStatus.cooling), valueClass: 'text-amber-600' },
    { key: 'runtime-invalid', label: '异常', value: optionalMetricNumber(runtimeStatus.invalid), valueClass: 'text-rose-600' },
    { key: 'runtime-disabled', label: '禁用', value: optionalMetricNumber(runtimeStatus.disabled), valueClass: 'text-muted-foreground' },
    { key: 'calls-total', label: '调用总数', value: optionalMetricNumber(summary.calls_total), valueClass: '' },
  ]
  for (const metric of runtimeMetrics) {
    if (metric.value === null) continue
    items.push({ ...metric, value: metric.value })
  }

  const quota = summary.quota || {}
  for (const mode of Object.keys(grokQuotaMetricLabels) as GrokQuotaMode[]) {
    const value = optionalMetricNumber(quota[mode])
    if (value === null) continue
    items.push({ key: `quota-${mode}`, label: grokQuotaMetricLabels[mode], value })
  }
  return items
})

const grokOAuthTotal = computed(() => safeMetricNumber(grokSummary.value?.oauth_total))

function setActiveAccountPlatform(value: string) {
  if (value !== 'gpt' && value !== 'grok') return
  activeAccountPlatform.value = value
  if (value !== 'grok') grokCredentialsAccount.value = null
  if (value !== 'grok') showGrokOAuthAccess.value = false
  if (value !== 'grok' && !grokChattingAccountId.value) grokConversationAccount.value = null
  if (value === 'grok' && !grokLoading.value && grokAccounts.value.length === 0) {
    void loadGrokAccounts({ silentErrorToast: true })
  }
}

async function handleGrokExportAction(format: string) {
  if (format === 'json' || format === 'txt') {
    await exportGrokAccounts(format)
  }
}

async function syncGrokAccount(item: GrokAccount) {
  await syncGrokAccounts([item.id])
}

async function refreshGrokAccount(item: GrokAccount) {
  await refreshGrokRuntime([item.id])
}

async function testGrokAccount(item: GrokAccount) {
  await testGrokAccountValidity(item)
}

function normalizeGrokBatchChatTestSummary(summary: Partial<GrokAccountsBatchChatTestSummary> | null | undefined) {
  return {
    total: safeMetricNumber(summary?.total),
    success: safeMetricNumber(summary?.success),
    blocked: safeMetricNumber(summary?.blocked),
    invalid: safeMetricNumber(summary?.invalid),
    limited: safeMetricNumber(summary?.limited),
    permission: safeMetricNumber(summary?.permission),
    failed: safeMetricNumber(summary?.failed),
    skipped: safeMetricNumber(summary?.skipped),
  }
}

function grokBatchChatTestSummaryText(summary: ReturnType<typeof normalizeGrokBatchChatTestSummary>) {
  return `成功 ${summary.success}，封禁 ${summary.blocked}，登录失效 ${summary.invalid}，限流 ${summary.limited}，权限 ${summary.permission}，失败 ${summary.failed}，跳过 ${summary.skipped}`
}

function grokBatchChatTestJobStatusText(status: string) {
  return ({
    queued: '排队中',
    running: '正在执行',
    completed: '已完成',
    cancelled: '已停止',
    failed: '失败',
  } as Record<string, string>)[status] || status || '正在执行'
}

function grokBatchChatTestResultStatusText(status: string) {
  return ({
    blocked: '已封禁',
    invalid: '登录失效',
    limited: '限流（非封禁）',
    permission: 'Console 无权限',
    failed: '测试失败 / 无法判断',
    skipped: '跳过',
  } as Record<string, string>)[status] || status || '未知'
}

function grokBatchChatTestResultStatusClass(status: string) {
  return ({
    blocked: 'text-rose-600',
    invalid: 'text-rose-600',
    limited: 'text-amber-600',
    permission: 'text-sky-600',
    failed: 'text-rose-600',
    skipped: 'text-muted-foreground',
  } as Record<string, string>)[status] || 'text-muted-foreground'
}

function isGrokBatchChatTestTerminal(status: string) {
  return status === 'completed' || status === 'cancelled' || status === 'failed'
}

const grokBatchChatTestCanCancel = computed(() => (
  grokBatchChatTestProgress.busy
  && Boolean(grokBatchChatTestProgress.jobId)
  && !grokBatchChatTestProgress.cancelRequested
  && ['queued', 'running'].includes(grokBatchChatTestProgress.jobStatus)
))

const grokBatchChatTestFailureDetails = computed(() => (
  grokBatchChatTestProgress.results
    .filter((item) => !['success', 'pending'].includes(String(item.status || '').toLowerCase()))
    .sort((left, right) => {
      const priority: Record<string, number> = { blocked: 0, invalid: 1, permission: 2, failed: 3, limited: 4, skipped: 5 }
      return (priority[String(left.status || '').toLowerCase()] ?? 9) - (priority[String(right.status || '').toLowerCase()] ?? 9)
    })
    .slice(0, 20)
))

function stopGrokBatchChatTestPolling() {
  grokBatchChatTestPollVersion += 1
  if (grokBatchChatTestPollTimer !== null) {
    window.clearTimeout(grokBatchChatTestPollTimer)
    grokBatchChatTestPollTimer = null
  }
}

function applyGrokBatchChatTestJob(job: GrokAccountsBatchChatTestJob) {
  const status = String(job.status || '').toLowerCase()
  const summary = normalizeGrokBatchChatTestSummary(job.summary)
  const total = Math.max(safeMetricNumber(job.total), summary.total)
  const current = Math.min(total, safeMetricNumber(job.current))
  const results = Array.isArray(job.results) ? job.results : []
  const summaryText = grokBatchChatTestSummaryText(summary)

  grokBatchChatTestProgress.jobId = String(job.id || '').trim()
  grokBatchChatTestProgress.jobStatus = status
  grokBatchChatTestProgress.currentId = String(job.current_id || '').trim()
  grokBatchChatTestProgress.total = total
  grokBatchChatTestProgress.current = current
  grokBatchChatTestProgress.statusLabel = grokBatchChatTestJobStatusText(status)
  grokBatchChatTestProgress.results = results
  grokBatchChatTestProgress.cancelRequested = Boolean(job.cancel_requested)
  grokBatchChatTestProgress.message = isGrokBatchChatTestTerminal(status)
    ? summaryText
    : `${grokBatchChatTestJobStatusText(status)}：${current} / ${total}${grokBatchChatTestProgress.currentId ? `；当前 ${grokBatchChatTestProgress.currentId}` : ''}；${summaryText}`
  grokBatchChatTestProgress.error = status === 'failed'
    ? String(job.error || '全部对话测试任务失败')
    : ''
}

async function finalizeGrokBatchChatTest(job: GrokAccountsBatchChatTestJob) {
  const jobId = String(job.id || '').trim()
  if (!jobId || grokBatchChatTestFinalizedJobId === jobId) return
  grokBatchChatTestFinalizedJobId = jobId
  stopGrokBatchChatTestPolling()
  applyGrokBatchChatTestJob(job)
  grokBatchChatTestProgress.busy = false
  grokBatchChatTestProgress.cancelRequested = false

  const summary = normalizeGrokBatchChatTestSummary(job.summary)
  const summaryText = grokBatchChatTestSummaryText(summary)
  const status = String(job.status || '').toLowerCase()
  if (status === 'completed') {
    if (summary.limited || summary.permission || summary.failed) {
      toast.warning(`全部对话测试完成：${summaryText}`)
    } else {
      toast.success(`全部对话测试完成：${summaryText}`)
    }
  } else if (status === 'cancelled') {
    toast.info(`全部对话测试已停止：${summaryText}`)
  } else {
    toast.error(grokBatchChatTestProgress.error || '全部对话测试任务失败')
  }

  await loadGrokAccounts({ silentErrorToast: true })
}

function scheduleGrokBatchChatTestPoll(jobId: string, version: number) {
  if (version !== grokBatchChatTestPollVersion || !grokBatchChatTestProgress.busy) return
  if (grokBatchChatTestPollTimer !== null) window.clearTimeout(grokBatchChatTestPollTimer)
  grokBatchChatTestPollTimer = window.setTimeout(() => {
    grokBatchChatTestPollTimer = null
    void pollGrokBatchChatTest(jobId, version)
  }, GROK_BATCH_CHAT_TEST_POLL_INTERVAL_MS)
}

async function pollGrokBatchChatTest(jobId: string, version: number) {
  if (version !== grokBatchChatTestPollVersion || !grokBatchChatTestProgress.busy) return
  try {
    const result = await grokAccountsApi.getBatchChatTestJob(jobId)
    if (version !== grokBatchChatTestPollVersion) return
    const job = result.job
    applyGrokBatchChatTestJob(job)
    if (isGrokBatchChatTestTerminal(String(job.status || '').toLowerCase())) {
      await finalizeGrokBatchChatTest(job)
      return
    }
  } catch (error) {
    if (version !== grokBatchChatTestPollVersion) return
    const message = errorMessage(error, '读取全部对话测试进度失败')
    const status = Number((error as { status?: unknown })?.status || 0)
    if ([401, 403, 404].includes(status)) {
      stopGrokBatchChatTestPolling()
      grokBatchChatTestProgress.busy = false
      grokBatchChatTestProgress.cancelRequested = false
      grokBatchChatTestProgress.statusLabel = '失败'
      grokBatchChatTestProgress.error = message
      toast.error(message, '全部对话测试失败')
      await loadGrokAccounts({ silentErrorToast: true })
      return
    }
    grokBatchChatTestProgress.message = `${message}，正在重试...`
  }
  scheduleGrokBatchChatTestPoll(jobId, version)
}

async function cancelGrokBatchChatTest() {
  const jobId = grokBatchChatTestProgress.jobId
  if (!jobId || !grokBatchChatTestCanCancel.value) return
  grokBatchChatTestProgress.cancelRequested = true
  grokBatchChatTestProgress.message = '正在请求停止全部对话测试...'
  try {
    const result = await grokAccountsApi.cancelBatchChatTestJob(jobId)
    const job = result.job
    applyGrokBatchChatTestJob(job)
    if (isGrokBatchChatTestTerminal(String(job.status || '').toLowerCase())) {
      await finalizeGrokBatchChatTest(job)
    }
  } catch (error) {
    grokBatchChatTestProgress.cancelRequested = false
    grokBatchChatTestProgress.error = errorMessage(error, '停止全部对话测试失败')
    toast.error(grokBatchChatTestProgress.error)
  }
}

async function runGrokBatchChatTest() {
  if (
    grokBatchChatTestProgress.busy
    || grokConversationAccount.value
    || grokChattingAccountId.value
  ) return
  if (!grokAccountAllTotal.value) {
    toast.warning('暂无可测试的 Grok 账号')
    return
  }
  const confirmed = await confirmDialog.ask({
    title: '确认全部对话测试',
    message: `将逐个对全部保存 SSO 登录态的 Grok 账号发起一次 Console 对话测试，固定使用 ${GROK_BATCH_CHAT_TEST_MODEL}。每个可测账号都会消耗一次 Console 对话额度；没有 SSO 登录态的账号会跳过。是否继续？`,
    confirmText: '开始测试',
    cancelText: '取消',
  })
  if (!confirmed) return

  Object.assign(grokBatchChatTestProgress, {
    open: true,
    title: '全部 Grok 对话测试',
    subtitle: `固定模型：${GROK_BATCH_CHAT_TEST_MODEL}`,
    total: 0,
    current: 0,
    statusLabel: '正在创建任务',
    message: '正在创建后台测试任务...',
    error: '',
    busy: true,
    jobId: '',
    jobStatus: 'queued',
    currentId: '',
    cancelRequested: false,
    results: [],
  })
  grokBatchChatTestFinalizedJobId = ''
  stopGrokBatchChatTestPolling()
  const version = grokBatchChatTestPollVersion

  try {
    const result = await grokAccountsApi.startBatchChatTest({
      prompt: GROK_BATCH_CHAT_TEST_PROMPT,
      model: GROK_BATCH_CHAT_TEST_MODEL,
    })
    const job = result.job
    if (!job || !String(job.id || '').trim()) throw new Error('后台测试任务未返回任务 ID')
    applyGrokBatchChatTestJob(job)
    if (isGrokBatchChatTestTerminal(String(job.status || '').toLowerCase())) {
      await finalizeGrokBatchChatTest(job)
      return
    }
    scheduleGrokBatchChatTestPoll(String(job.id), version)
  } catch (error) {
    const message = errorMessage(error, '全部对话测试失败')
    grokBatchChatTestProgress.statusLabel = '失败'
    grokBatchChatTestProgress.error = message
    grokBatchChatTestProgress.busy = false
    toast.error(message, '全部对话测试失败')
  }
}

onBeforeUnmount(stopGrokBatchChatTestPolling)

function openGrokConversationTest(item: GrokAccount) {
  if (!item.has_sso || grokChattingAccountId.value) return
  grokConversationAccount.value = item
}

function closeGrokConversationTest() {
  if (grokChattingAccountId.value) return
  grokConversationAccount.value = null
}

function setGrokConversationRunning(accountId: string, running: boolean) {
  if (running) {
    grokChattingAccountId.value = accountId
    return
  }
  if (grokChattingAccountId.value !== accountId) return
  grokChattingAccountId.value = ''
  void loadGrokAccounts({ silentErrorToast: true }).then(() => {
    const refreshed = grokAccounts.value.find((item) => item.id === accountId)
    if (refreshed && grokConversationAccount.value?.id === accountId) {
      grokConversationAccount.value = refreshed
    }
  })
}

async function toggleGrokAccountDisabled(item: GrokAccount) {
  const disabled = String(item.runtime_status || '').toLowerCase() !== 'disabled'
  await setGrokRuntimeDisabled([item.id], disabled)
}

function grokOAuthActionFor(item: GrokAccount) {
  if (!grokOAuthRowAction.accountId) return ''
  return grokOAuthRowAction.accountId === item.id ? grokOAuthRowAction.action : 'busy'
}

async function runGrokOAuthAccountAction(item: GrokAccount, action: GrokOAuthRowAction) {
  const oauth = item.oauth
  const panel = grokOAuthPanelRef.value
  if (!oauth || !panel || grokOAuthRowAction.accountId) return
  grokOAuthRowAction.accountId = item.id
  grokOAuthRowAction.action = action
  try {
    if (action === 'sync') await panel.syncModels(oauth.id)
    if (action === 'refresh') await panel.refreshAccount(oauth.id)
    if (action === 'toggle') await panel.setDisabled(oauth)
    if (action === 'remove') await panel.removeAccount(oauth.id)
  } finally {
    grokOAuthRowAction.accountId = ''
    grokOAuthRowAction.action = ''
  }
}

function syncGrokOAuthAccount(item: GrokAccount) {
  return runGrokOAuthAccountAction(item, 'sync')
}

function refreshGrokOAuthAccount(item: GrokAccount) {
  return runGrokOAuthAccountAction(item, 'refresh')
}

function toggleGrokOAuthAccount(item: GrokAccount) {
  return runGrokOAuthAccountAction(item, 'toggle')
}

async function removeGrokOAuthAccount(item: GrokAccount) {
  if (!item.oauth) return
  const confirmed = await confirmDialog.ask({
    title: '移除 OAuth',
    message: `将移除 ${item.email || item.id} 的 OAuth 凭据，注册账号和 SSO 登录态会保留。是否继续？`,
    confirmText: '移除 OAuth',
    cancelText: '取消',
  })
  if (confirmed) await runGrokOAuthAccountAction(item, 'remove')
}

function handleGrokOAuthChanged() {
  void loadGrokAccounts({ silentErrorToast: true, silentLoading: true })
}

function openGrokLoginCredentials(item: GrokAccount) {
  if (!item.has_password) return
  grokCredentialsAccount.value = item
}

const manualTokenFileInputRef = ref<HTMLInputElement | null>(null)
const cpaFileInputRef = ref<HTMLInputElement | null>(null)
const remoteImportBusy = ref(false)
const accountToolbarMenuClass = 'shrink-0 whitespace-nowrap'
const accountToolbarButtonClass = 'shrink-0 whitespace-nowrap justify-between gap-2'
const accountStatusDetailCardClass = 'w-72 account-status-detail-card'
const accountToolbarSecondaryClass = `${accountToolbarButtonClass} text-muted-foreground`
const importModalBusy = computed(() => importBusy.value || remoteImportBusy.value)

const accountGroupNameMap = computed(() => buildAccountGroupNameMap(accountGroups.value))

const accountGroupRows = computed(() => buildAccountGroupRows(accountGroups.value, proxyGroups.value))

function accountGroupLabel(groupId: string | undefined) {
  return buildAccountGroupLabel(groupId, accountGroupNameMap.value)
}

function accountStatusDetailText(item: Account) {
  return buildAccountStatusDetailText(item, accountGroupLabel, accountProxyText)
}

const refreshProgressItems = computed(() => buildAccountProgressMetricItems(
  refreshProgressMetricLabel.value,
  refreshProgressMetricValue.value,
  refreshProgressStatusText.value,
))

const {
  accountEntryItems,
  exportMenuItems,
  batchMenuItems,
  toolbarBatchMenuItems,
  handleBatchAction,
  handleToolbarBatchAction,
  handleAccountEntryAction,
  handleExportAction,
} = useAccountActionMenuRuntime({
  selectedCount,
  accountAllTotal,
  accountGroupsLoading,
  bindAccountGroupOptions,
  selectedBindGroupId,
  openCreateModal,
  openImportModal,
  exportAccounts,
  refreshAllAccounts,
  extractSelectedCheckout,
  runBulkAction,
  bindSelectedAccountsToGroup,
})

function openManualTokenFile() {
  if (!manualTokenFileInputRef.value || importBusy.value) return
  manualTokenFileInputRef.value.value = ''
  manualTokenFileInputRef.value.click()
}

async function handleManualTokenFileChange(event: Event) {
  const target = event.target as HTMLInputElement | null
  const file = target?.files?.[0]
  await importTokenTextFile(file)
  if (target) target.value = ''
}

function openCPAFileDialog() {
  if (!cpaFileInputRef.value || importBusy.value) return
  cpaFileInputRef.value.value = ''
  cpaFileInputRef.value.click()
}

async function handleCPAFileChange(event: Event) {
  const target = event.target as HTMLInputElement | null
  await importLocalCPAFiles(target?.files)
  if (target) target.value = ''
}

function handleRemoteImportDone() {
  void loadData({ silentErrorToast: true })
}
</script>

<style scoped>
.accounts-toolbar {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.accounts-toolbar-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.accounts-toolbar-row-main {
  justify-content: space-between;
}

.accounts-toolbar-row-actions {
  align-items: flex-start;
  justify-content: space-between;
  padding-top: 10px;
  border-top: 1px solid hsl(var(--border) / 0.62);
}

.accounts-toolbar-filters {
  min-width: min(100%, 34rem);
  flex: 1 1 34rem;
}

.accounts-toolbar-summary {
  display: flex;
  flex: 0 0 auto;
  justify-content: flex-end;
}

.accounts-toolbar-group {
  min-width: 0;
}

.accounts-toolbar-action-cluster {
  display: flex;
  min-width: 0;
  flex: 1 1 auto;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
}

.accounts-toolbar-group-ops {
  flex: 0 1 auto;
}

.accounts-toolbar-group-refresh {
  margin-left: auto;
  justify-content: flex-end;
}

@media (max-width: 900px) {
  .accounts-toolbar-summary {
    width: 100%;
    justify-content: flex-start;
  }

  .accounts-toolbar-group-refresh {
    width: 100%;
    margin-left: 0;
    justify-content: flex-start;
  }
}
</style>
