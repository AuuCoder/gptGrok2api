<template>
  <div class="register-task-settings">
    <FormSection title="注册目标" density="roomy">
      <fieldset class="register-target-segmented" :disabled="config.enabled">
        <SegmentedTabs
          :model-value="config.target"
          :options="targetOptions"
          aria-label="注册目标"
          @update:model-value="emit('update-target', String($event))"
        />
      </fieldset>
    </FormSection>

    <FormSection title="任务参数" density="roomy">
      <div class="register-form-grid">
        <label class="register-field">
          <span class="register-label">任务模式</span>
          <GroupedSelectMenu
            v-model="config.mode"
            :groups="registerModeGroups"
            selected-indicator="none"
            :disabled="config.enabled || config.target === 'grok'"
            block
          />
        </label>

        <label v-if="config.mode === 'total'" class="register-field">
          <span class="register-label">注册总数</span>
          <Input
            v-model.number="config.total"
            type="number"
            min="1"
            block
            :disabled="config.enabled || config.mode !== 'total'"
          />
        </label>

        <label v-else-if="config.mode === 'quota'" class="register-field">
          <span class="register-label">目标剩余额度</span>
          <Input
            v-model.number="config.target_quota"
            type="number"
            min="1"
            block
            :disabled="config.enabled"
          />
        </label>

        <label v-else class="register-field">
          <span class="register-label">目标可用账号</span>
          <Input
            v-model.number="config.target_available"
            type="number"
            min="1"
            block
            :disabled="config.enabled"
          />
        </label>

        <fieldset class="checkout-channel-segmented register-field--full" :disabled="config.enabled || !config.checkout.enabled">
          <legend class="register-label">提链渠道</legend>
          <SegmentedTabs
            :model-value="config.checkout.channel"
            :options="checkoutChannelOptions"
            aria-label="提链渠道"
            @update:model-value="updateCheckoutChannel(String($event))"
          />
        </fieldset>

        <label class="register-field">
          <span class="register-label">线程数</span>
          <Input
            v-model.number="config.threads"
            type="number"
            min="1"
            block
            :disabled="config.enabled"
          />
        </label>

        <label v-if="config.mode !== 'total'" class="register-field">
          <span class="register-label">检查间隔（秒）</span>
          <Input
            v-model.number="config.check_interval"
            type="number"
            min="1"
            block
            :disabled="config.enabled"
          />
        </label>

        <label class="register-field">
          <span class="register-label">注册代理</span>
          <GroupedSelectMenu
            :model-value="proxyMode"
            :groups="registerProxyModeGroups"
            selected-indicator="none"
            :disabled="config.enabled"
            block
            @update:model-value="emit('update-proxy-mode', $event)"
          />
        </label>

        <label v-if="proxyMode === 'group'" class="register-field">
          <span class="register-label">代理组</span>
          <GroupedSelectMenu
            :model-value="selectedProxyGroupId"
            :groups="proxyGroupGroups"
            selected-indicator="none"
            :disabled="config.enabled"
            block
            @update:model-value="emit('select-proxy-group', $event)"
          />
        </label>

        <label v-else-if="proxyMode === 'custom'" class="register-field">
          <span class="register-label">自定义代理</span>
          <Input
            :model-value="customProxyInput"
            block
            root-class="font-mono"
            placeholder="http://127.0.0.1:7890"
            :disabled="config.enabled"
            @update:model-value="emit('update-custom-proxy', $event)"
          />
        </label>

        <p class="register-proxy-hint register-field--full">
          {{ proxyHint }}
        </p>
      </div>
    </FormSection>

    <FormSection v-if="config.target === 'openai'" title="Checkout / 支付链接" density="roomy">
      <div class="register-form-grid">
        <label class="register-toggle register-field--full">
          <Checkbox
            :model-value="config.checkout.enabled"
            :disabled="config.enabled"
            @update:model-value="config.checkout.enabled = Boolean($event)"
          />
          <span>
            <strong>注册成功后自动提链</strong>
          </span>
        </label>

        <fieldset
          v-if="config.checkout.channel === 'pix'"
          class="checkout-channel-segmented pix-protocol-segmented register-field--full"
          :disabled="config.enabled || !config.checkout.enabled"
        >
          <legend class="register-label">Pix 提链方案</legend>
          <SegmentedTabs
            :model-value="config.checkout.pix_protocol"
            :options="pixProtocolOptions"
            aria-label="Pix 提链方案"
            @update:model-value="updatePixProtocol(String($event))"
          />
        </fieldset>

        <label class="register-field">
          <span class="register-label">提链线程数</span>
          <Input
            v-model.number="config.checkout.threads"
            type="number"
            min="1"
            block
            :disabled="config.enabled || !config.checkout.enabled"
          />
        </label>

        <label class="register-toggle register-field--full">
          <Checkbox
            :model-value="config.checkout.continuous_retry !== false"
            :disabled="config.enabled || !config.checkout.enabled"
            @update:model-value="config.checkout.continuous_retry = Boolean($event)"
          />
          <span>
            <strong>失败后持续换代理提链</strong>
            <small>{{ continuousRetryHint }}</small>
          </span>
        </label>

        <p class="checkout-channel-flow register-field--full">
          {{ checkoutProxyPlan.flow }}
        </p>

        <fieldset class="checkout-proxy-stage register-field--full">
          <legend>{{ checkoutProxyPlan.checkout.legend }}</legend>
          <label class="register-toggle">
            <Checkbox
              :model-value="config.checkout.checkout_proxy_enabled"
              :disabled="config.enabled || !config.checkout.enabled"
              @update:model-value="config.checkout.checkout_proxy_enabled = Boolean($event)"
            />
            <span>
              <strong>使用独立出口</strong>
              <small>{{ checkoutProxyPlan.checkout.description }}</small>
            </span>
          </label>

          <div v-if="config.checkout.checkout_proxy_enabled" class="register-field">
            <div class="checkout-proxy-field-header">
              <span class="register-label">{{ checkoutProxyPlan.checkout.label }}</span>
              <Button
                size="xs"
                variant="outline"
                :disabled="config.enabled || !config.checkout.enabled || checkoutProxyTesting === 'checkout' || !config.checkout.checkout_proxy_url.trim()"
                @click="testCheckoutProxy('checkout')"
              >
                {{ checkoutProxyTesting === 'checkout' ? '测试中...' : '抽样测试' }}
              </Button>
            </div>
            <textarea
              v-model="config.checkout.checkout_proxy_url"
              rows="4"
              autocomplete="off"
              spellcheck="false"
              class="ui-textarea-sm resize-y font-mono"
              placeholder="每行一个代理，支持 URL 或 host:port:user:password"
              title="支持 URL、host:port:user:password、user:password:host:port、user:password@host:port、host:port@user:password"
              :disabled="config.enabled || !config.checkout.enabled || checkoutProxyTesting === 'checkout'"
              @input="clearCheckoutProxyTest('checkout')"
            ></textarea>
            <p
              v-if="checkoutProxyTestResults.checkout"
              class="checkout-proxy-test-result"
              :class="checkoutProxyTestResults.checkout.ok ? 'checkout-proxy-test-result--success' : 'checkout-proxy-test-result--error'"
              role="status"
              aria-live="polite"
            >
              {{ checkoutProxyTestText('checkout') }}
            </p>
          </div>
        </fieldset>

        <fieldset v-if="usesPromotionProxy" class="checkout-proxy-stage register-field--full">
          <legend>{{ checkoutProxyPlan.promotion?.legend }}</legend>
          <label class="register-toggle">
            <Checkbox
              :model-value="config.checkout.promotion_proxy_enabled"
              :disabled="config.enabled || !config.checkout.enabled"
              @update:model-value="config.checkout.promotion_proxy_enabled = Boolean($event)"
            />
            <span>
              <strong>使用独立出口</strong>
              <small>{{ checkoutProxyPlan.promotion?.description }}</small>
            </span>
          </label>

          <div v-if="config.checkout.promotion_proxy_enabled" class="register-field">
            <div class="checkout-proxy-field-header">
              <span class="register-label">{{ checkoutProxyPlan.promotion?.label }}</span>
              <Button
                size="xs"
                variant="outline"
                :disabled="config.enabled || !config.checkout.enabled || checkoutProxyTesting === 'promotion' || !config.checkout.promotion_proxy_url.trim()"
                @click="testCheckoutProxy('promotion')"
              >
                {{ checkoutProxyTesting === 'promotion' ? '测试中...' : '抽样测试' }}
              </Button>
            </div>
            <textarea
              v-model="config.checkout.promotion_proxy_url"
              rows="4"
              autocomplete="off"
              spellcheck="false"
              class="ui-textarea-sm resize-y font-mono"
              placeholder="每行一个代理，支持 URL 或 host:port:user:password"
              title="支持 URL、host:port:user:password、user:password:host:port、user:password@host:port、host:port@user:password"
              :disabled="config.enabled || !config.checkout.enabled || checkoutProxyTesting === 'promotion'"
              @input="clearCheckoutProxyTest('promotion')"
            ></textarea>
            <p
              v-if="checkoutProxyTestResults.promotion"
              class="checkout-proxy-test-result"
              :class="checkoutProxyTestResults.promotion.ok ? 'checkout-proxy-test-result--success' : 'checkout-proxy-test-result--error'"
              role="status"
              aria-live="polite"
            >
              {{ checkoutProxyTestText('promotion') }}
            </p>
          </div>
        </fieldset>

      </div>
    </FormSection>

    <FormSection
      v-if="config.target === 'openai'"
      title="外部同步"
      density="roomy"
      :class="{ 'register-collapsible-section--collapsed': !sub2apiExpanded }"
    >
      <template #actions>
        <Button
          size="sm"
          variant="outline"
          :disabled="sub2apiLoading || openAICPALoading"
          @click="refreshOpenAIConnections"
        >
          {{ sub2apiLoading || openAICPALoading ? '刷新中...' : '刷新连接' }}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          icon-only
          root-class="h-8 w-8 shrink-0"
          :title="sub2apiExpanded ? '收起外部同步' : '展开外部同步'"
          :aria-label="sub2apiExpanded ? '收起外部同步' : '展开外部同步'"
          :aria-expanded="sub2apiExpanded"
          aria-controls="register-sub2api-settings"
          @click="sub2apiExpanded = !sub2apiExpanded"
        >
          <Icon
            :icon="sub2apiExpanded ? 'lucide:chevron-up' : 'lucide:chevron-down'"
            class="h-4 w-4"
          />
        </Button>
      </template>

      <div v-show="sub2apiExpanded" id="register-sub2api-settings" class="register-form-grid">
        <label class="register-toggle register-field--full">
          <Checkbox
            :model-value="config.sub2api_sync.enabled"
            :disabled="config.enabled"
            @update:model-value="updateSub2APIEnabled"
          />
          <span>
            <strong>注册成功后同步到 Sub2API</strong>
            <small>账号先保存到本地，再同步到所选的 Sub2API 连接；同步失败不会影响注册结果。</small>
          </span>
        </label>

        <template v-if="config.sub2api_sync.enabled">
          <p v-if="sub2apiLoadError" class="sub2api-sync-message register-field--full sub2api-sync-message--error">
            {{ sub2apiLoadError }}
          </p>

          <p v-if="!sub2apiServers.length && !sub2apiLoading" class="sub2api-sync-message register-field--full">
            尚未配置 Sub2API 连接。请先在“设置 / 外部来源”添加连接，再返回这里选择。
          </p>

          <template v-if="sub2apiServers.length || sub2apiLoading">
            <label class="register-field register-field--full">
              <span class="register-label">Sub2API 连接</span>
              <GroupedSelectMenu
                :model-value="config.sub2api_sync.server_id"
                :groups="sub2apiServerGroups"
                selected-indicator="none"
                :disabled="config.enabled || sub2apiLoading"
                block
                @update:model-value="updateSub2APIServer"
              />
            </label>

            <template v-if="config.sub2api_sync.server_id">
              <label class="register-field">
                <span class="register-label">远端分组方式</span>
                <GroupedSelectMenu
                  :model-value="config.sub2api_sync.group_mode"
                  :groups="sub2apiGroupModeGroups"
                  selected-indicator="none"
                  :disabled="config.enabled"
                  block
                  @update:model-value="updateSub2APIGroupMode"
                />
              </label>

              <label v-if="config.sub2api_sync.group_mode === 'existing'" class="register-field">
                <span class="register-label">远端分组</span>
                <GroupedSelectMenu
                  :model-value="config.sub2api_sync.group_id"
                  :groups="sub2apiRemoteGroupGroups"
                  selected-indicator="none"
                  :disabled="config.enabled || sub2apiGroupsLoading"
                  block
                  @update:model-value="updateSub2APIRemoteGroup"
                />
              </label>

              <label v-else class="register-field">
                <span class="register-label">自定义远端分组</span>
                <Input
                  :model-value="config.sub2api_sync.group_name"
                  block
                  placeholder="例如：新注册 GPT"
                  :disabled="config.enabled"
                  @update:model-value="updateSub2APIGroupName"
                />
              </label>

              <p v-if="config.sub2api_sync.group_mode === 'existing' && !sub2apiGroupsLoading && !sub2apiRemoteGroups.length" class="sub2api-sync-message register-field--full">
                未加载到远端分组。请刷新连接或切换为“自定义分组”。
              </p>
              <p v-else-if="config.sub2api_sync.group_mode === 'custom'" class="sub2api-sync-message register-field--full">
                首次同步时将创建或复用同名远端分组。
              </p>
            </template>

            <p v-else class="sub2api-sync-message register-field--full">
              请选择一个已保存的 Sub2API 连接。
            </p>
          </template>
        </template>

        <div class="register-field--full border-t border-border pt-4"></div>

        <label class="register-toggle register-field--full">
          <Checkbox
            :model-value="config.cpa_sync.enabled"
            :disabled="config.enabled"
            @update:model-value="updateOpenAICPAEnabled"
          />
          <span>
            <strong>注册成功后上传到 CPA</strong>
            <small>上传 CLIProxyAPI 兼容的 codex OAuth JSON；上传失败不会影响本地注册结果。</small>
          </span>
        </label>

        <template v-if="config.cpa_sync.enabled">
          <p v-if="openAICPALoadError" class="sub2api-sync-message register-field--full sub2api-sync-message--error">
            {{ openAICPALoadError }}
          </p>

          <p v-if="!cpaPools.length && !openAICPALoading" class="sub2api-sync-message register-field--full">
            尚未配置 CPA 连接。请先在“设置 / 外部来源”添加连接，再返回这里选择。
          </p>

          <label v-if="cpaPools.length || openAICPALoading" class="register-field register-field--full">
            <span class="register-label">CPA 连接</span>
            <GroupedSelectMenu
              :model-value="config.cpa_sync.pool_id"
              :groups="openAICPAPoolGroups"
              selected-indicator="none"
              :disabled="config.enabled || openAICPALoading"
              block
              @update:model-value="updateOpenAICPAPool"
            />
          </label>
        </template>

      </div>
    </FormSection>

    <FormSection v-if="config.target === 'grok'" title="Grok 注册协议" density="roomy">
      <div class="register-form-grid register-form-grid--grok">
        <label class="register-field">
          <span class="register-label">Turnstile 服务</span>
          <GroupedSelectMenu
            v-model="config.grok.provider"
            :groups="turnstileProviderGroups"
            selected-indicator="none"
            :disabled="config.enabled"
            block
          />
        </label>

        <label v-if="config.grok.provider !== 'local'" class="register-field">
          <span class="register-label">API Key</span>
          <Input
            v-model.trim="config.grok.api_key"
            type="password"
            autocomplete="off"
            block
            root-class="font-mono"
            :disabled="config.enabled"
          />
        </label>

        <label class="register-field">
          <span class="register-label">API Base</span>
          <Input
            v-model.trim="config.grok.api_base"
            block
            root-class="font-mono"
            :placeholder="turnstileApiBasePlaceholder(config.grok.provider)"
            :disabled="config.enabled"
          />
        </label>

        <label class="register-field">
          <span class="register-label">HTTP 超时（秒）</span>
          <Input
            v-model.number="config.grok.request_timeout"
            type="number"
            min="1"
            block
            :disabled="config.enabled"
          />
        </label>

        <label class="register-field">
          <span class="register-label">解题超时（秒）</span>
          <Input
            v-model.number="config.grok.captcha_timeout"
            type="number"
            min="1"
            block
            :disabled="config.enabled"
          />
        </label>

        <label class="register-field">
          <span class="register-label">轮询间隔（秒）</span>
          <Input
            v-model.number="config.grok.captcha_poll_interval"
            type="number"
            min="1"
            max="60"
            step="1"
            block
            :disabled="config.enabled"
          />
        </label>

        <template v-if="config.grok.provider === 'custom'">
          <label class="register-field">
            <span class="register-label">创建任务路径</span>
            <Input
              v-model.trim="config.grok.create_path"
              block
              root-class="font-mono"
              placeholder="/createTask"
              :disabled="config.enabled"
            />
          </label>

          <label class="register-field">
            <span class="register-label">查询结果路径</span>
            <Input
              v-model.trim="config.grok.result_path"
              block
              root-class="font-mono"
              placeholder="/getTaskResult"
              :disabled="config.enabled"
            />
          </label>
        </template>

        <label class="register-field">
          <span class="register-label">邮箱重试次数</span>
          <Input
            v-model.number="config.grok.max_mail_retries"
            type="number"
            min="1"
            max="20"
            block
            :disabled="config.enabled"
          />
        </label>

      </div>
    </FormSection>

    <FormSection v-if="config.target === 'grok'" title="Grok 运行时账号池" density="roomy">
      <div class="register-form-grid register-form-grid--grok">
        <p class="register-proxy-hint register-field--full">
          注册成功后会自动加入内置 Grok 运行时，并可继续完成 Grok 4.5 OAuth 协议授权。
        </p>

        <label class="register-toggle register-field--full">
          <Checkbox
            :model-value="config.grok.xai_cli_oauth_enabled"
            :disabled="config.enabled"
            @update:model-value="config.grok.xai_cli_oauth_enabled = Boolean($event)"
          />
          <span>
            <strong>注册后自动协议授权</strong>
            <small>默认开启；使用新账号完成 xAI Device Code OAuth，并加入 Grok 4.5 账号池。</small>
          </span>
        </label>

        <label class="register-field">
          <span class="register-label">账号池类型</span>
          <GroupedSelectMenu
            v-model="config.grok.grok2api_pool"
            :groups="grokPoolGroups"
            selected-indicator="none"
            :disabled="config.enabled"
            block
          />
        </label>

        <label class="register-toggle">
          <Checkbox
            :model-value="config.grok.grok2api_verify_on_import"
            :disabled="config.enabled"
            @update:model-value="config.grok.grok2api_verify_on_import = Boolean($event)"
          />
          <span>
            <strong>导入后刷新验证</strong>
            <small>加入运行池后检查登录态、运行状态和真实额度。</small>
          </span>
        </label>

        <label class="register-toggle">
          <Checkbox
            :model-value="config.grok.grok2api_auto_nsfw"
            :disabled="config.enabled"
            @update:model-value="config.grok.grok2api_auto_nsfw = Boolean($event)"
          />
          <span>
            <strong>导入后开启 NSFW</strong>
            <small>由内置 Grok 运行时完成检测与设置。</small>
          </span>
        </label>
      </div>
    </FormSection>

    <FormSection
      v-if="config.target === 'grok'"
      title="OAuth 投递"
      density="roomy"
      :class="{ 'register-collapsible-section--collapsed': !oauthDeliveryExpanded }"
    >
      <template #actions>
        <Button
          size="sm"
          variant="outline"
          :disabled="oauthDeliveryLoading"
          @click="refreshOAuthDeliveryConnections"
        >
          {{ oauthDeliveryLoading ? '刷新中...' : '刷新连接' }}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          icon-only
          root-class="h-8 w-8 shrink-0"
          :title="oauthDeliveryExpanded ? '收起 OAuth 投递' : '展开 OAuth 投递'"
          :aria-label="oauthDeliveryExpanded ? '收起 OAuth 投递' : '展开 OAuth 投递'"
          :aria-expanded="oauthDeliveryExpanded"
          aria-controls="register-grok-oauth-delivery"
          @click="oauthDeliveryExpanded = !oauthDeliveryExpanded"
        >
          <Icon
            :icon="oauthDeliveryExpanded ? 'lucide:chevron-up' : 'lucide:chevron-down'"
            class="h-4 w-4"
          />
        </Button>
      </template>

      <div v-show="oauthDeliveryExpanded" id="register-grok-oauth-delivery" class="register-form-grid">
        <p class="register-proxy-hint register-field--full">
          OAuth 凭据始终先保存到本地；两个远程目标可分别开启，投递失败不影响本地授权结果。
        </p>
        <p v-if="oauthDeliveryLoadError" class="sub2api-sync-message register-field--full sub2api-sync-message--error">
          {{ oauthDeliveryLoadError }}
        </p>

        <label class="register-toggle register-field--full">
          <Checkbox
            :model-value="config.grok.oauth_delivery.sub2api.enabled"
            :disabled="config.enabled"
            @update:model-value="config.grok.oauth_delivery.sub2api.enabled = Boolean($event)"
          />
          <span>
            <strong>上传到 Sub2API</strong>
            <small>使用 xAI OAuth 账号类型写入选定分组。</small>
          </span>
        </label>

        <template v-if="config.grok.oauth_delivery.sub2api.enabled">
          <p v-if="!sub2apiServers.length && !oauthDeliveryLoading" class="sub2api-sync-message register-field--full">
            尚未配置 Sub2API 连接。
          </p>
          <label v-else class="register-field register-field--full">
            <span class="register-label">Sub2API 连接</span>
            <GroupedSelectMenu
              :model-value="config.grok.oauth_delivery.sub2api.server_id"
              :groups="oauthDeliverySub2APIServerGroups"
              selected-indicator="none"
              :disabled="config.enabled || oauthDeliveryLoading"
              block
              @update:model-value="updateOAuthDeliverySub2APIServer"
            />
          </label>

          <template v-if="config.grok.oauth_delivery.sub2api.server_id">
            <label class="register-field">
              <span class="register-label">远程分组方式</span>
              <GroupedSelectMenu
                :model-value="config.grok.oauth_delivery.sub2api.group_mode"
                :groups="sub2apiGroupModeGroups"
                selected-indicator="none"
                :disabled="config.enabled"
                block
                @update:model-value="updateOAuthDeliveryGroupMode"
              />
            </label>
            <label v-if="config.grok.oauth_delivery.sub2api.group_mode === 'existing'" class="register-field">
              <span class="register-label">xAI 远程分组</span>
              <GroupedSelectMenu
                :model-value="config.grok.oauth_delivery.sub2api.group_id"
                :groups="oauthDeliveryRemoteGroupGroups"
                selected-indicator="none"
                :disabled="config.enabled || oauthDeliveryGroupsLoading"
                block
                @update:model-value="config.grok.oauth_delivery.sub2api.group_id = selectValue($event)"
              />
            </label>
            <label v-else class="register-field">
              <span class="register-label">自定义 xAI 分组</span>
              <Input
                v-model.trim="config.grok.oauth_delivery.sub2api.group_name"
                block
                placeholder="例如：Grok OAuth"
                :disabled="config.enabled"
              />
            </label>
          </template>
        </template>

        <label class="register-toggle register-field--full">
          <Checkbox
            :model-value="config.grok.oauth_delivery.cpa.enabled"
            :disabled="config.enabled"
            @update:model-value="config.grok.oauth_delivery.cpa.enabled = Boolean($event)"
          />
          <span>
            <strong>上传到 CPA</strong>
            <small>写入 CLIProxyAPI 兼容的 xAI OAuth JSON 文件。</small>
          </span>
        </label>

        <template v-if="config.grok.oauth_delivery.cpa.enabled">
          <p v-if="!cpaPools.length && !oauthDeliveryLoading" class="sub2api-sync-message register-field--full">
            尚未配置 CPA 连接。
          </p>
          <label v-else class="register-field register-field--full">
            <span class="register-label">CPA 连接</span>
            <GroupedSelectMenu
              :model-value="config.grok.oauth_delivery.cpa.pool_id"
              :groups="oauthDeliveryCPAPoolGroups"
              selected-indicator="none"
              :disabled="config.enabled || oauthDeliveryLoading"
              block
              @update:model-value="config.grok.oauth_delivery.cpa.pool_id = selectValue($event)"
            />
          </label>
        </template>
      </div>
    </FormSection>

    <FormSection
      title="邮箱请求"
      density="roomy"
      :class="{ 'register-collapsible-section--collapsed': !mailRequestExpanded }"
    >
      <template #actions>
        <Button
          size="sm"
          variant="ghost"
          icon-only
          root-class="h-8 w-8 shrink-0"
          :title="mailRequestExpanded ? '收起邮箱请求' : '展开邮箱请求'"
          :aria-label="mailRequestExpanded ? '收起邮箱请求' : '展开邮箱请求'"
          :aria-expanded="mailRequestExpanded"
          aria-controls="register-mail-request-settings"
          @click="mailRequestExpanded = !mailRequestExpanded"
        >
          <Icon
            :icon="mailRequestExpanded ? 'lucide:chevron-up' : 'lucide:chevron-down'"
            class="h-4 w-4"
          />
        </Button>
      </template>

      <div
        v-show="mailRequestExpanded"
        id="register-mail-request-settings"
        class="register-form-grid register-form-grid--mail"
      >
        <label class="register-field">
          <span class="register-label">请求超时（秒）</span>
          <Input
            v-model.number="config.mail.request_timeout"
            type="number"
            min="1"
            block
            :disabled="config.enabled"
          />
        </label>

        <label class="register-field">
          <span class="register-label">验证码等待（秒）</span>
          <Input
            v-model.number="config.mail.wait_timeout"
            type="number"
            min="1"
            block
            :disabled="config.enabled"
          />
        </label>

        <label class="register-field">
          <span class="register-label">轮询间隔（秒）</span>
          <Input
            v-model.number="config.mail.wait_interval"
            type="number"
            min="1"
            step="0.2"
            block
            :disabled="config.enabled"
          />
        </label>

        <label class="register-field register-field--full">
          <span class="register-label">请求 User-Agent</span>
          <Input
            v-model.trim="config.mail.user_agent"
            block
            root-class="font-mono"
            placeholder="留空使用默认 UA"
            :disabled="config.enabled"
          />
        </label>
      </div>
    </FormSection>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Icon } from '@iconify/vue'
import { Button, Checkbox, Input, SegmentedTabs } from 'nanocat-ui'

import FormSection from '@/components/ai/FormSection.vue'
import GroupedSelectMenu from '@/components/ui/GroupedSelectMenu.vue'
import { accountImportsApi } from '@/api/accountImports'
import type { CPAPool, Sub2APIRemoteGroup, Sub2APIServer } from '@/api/accountImports'
import { proxyApi, type ProxySampleTestResult } from '@/api/proxy'
import type { LegacyRegisterConfig } from '@/api/register'
import {
  registerModeGroups,
  registerProxyModeGroups,
  registerTargetOptions,
  turnstileApiBasePlaceholder,
  turnstileProviderGroups,
  type RegisterProxyMode,
} from '@/views/register/registerProviderView'

const targetOptions = registerTargetOptions.map(item => ({ ...item }))
const checkoutChannelOptions = [
  { value: 'upi', label: 'UPI' },
  { value: 'pix', label: 'Pix' },
]
const pixProtocolOptions = [
  { value: 'enhanced', label: '方案 1 · 当前' },
  { value: 'reference', label: '方案 2 · 参考' },
  { value: 'standalone', label: '方案 3 · BR/VN' },
]
const grokPoolGroups = [{
  options: [
    { value: 'auto', label: '自动检测（推荐）' },
    { value: 'basic', label: 'Basic' },
    { value: 'super', label: 'Super' },
    { value: 'heavy', label: 'Heavy' },
  ],
}]
const sub2apiGroupModeGroups = [{
  options: [
    { value: 'existing', label: '选择已有分组' },
    { value: 'custom', label: '自定义分组' },
  ],
}]
const props = defineProps<{
  config: LegacyRegisterConfig
  proxyMode: RegisterProxyMode
  selectedProxyGroupId: string
  customProxyInput: string
  proxyGroupGroups: unknown[]
  proxyHint: string
}>()

const sub2apiServers = ref<Sub2APIServer[]>([])
const sub2apiRemoteGroups = ref<Sub2APIRemoteGroup[]>([])
const sub2apiLoading = ref(false)
const sub2apiGroupsLoading = ref(false)
const sub2apiLoadError = ref('')
const sub2apiExpanded = ref(false)
const openAICPALoading = ref(false)
const openAICPALoadError = ref('')
const oauthDeliveryExpanded = ref(false)
const oauthDeliveryLoading = ref(false)
const oauthDeliveryGroupsLoading = ref(false)
const oauthDeliveryLoadError = ref('')
const oauthDeliveryRemoteGroups = ref<Sub2APIRemoteGroup[]>([])
const cpaPools = ref<CPAPool[]>([])
const mailRequestExpanded = ref(false)
let sub2apiGroupRequestVersion = 0
let oauthDeliveryGroupRequestVersion = 0

type CheckoutProxyTestStage = 'checkout' | 'promotion'

function updateCheckoutChannel(value: string) {
  const channel = value === 'pix' ? 'pix' : 'upi'
  props.config.checkout.channel = channel
  props.config.checkout.country = channel === 'pix' ? 'BR' : 'IN'
  props.config.checkout.currency = channel === 'pix' ? 'BRL' : 'INR'
  clearCheckoutProxyTest('checkout')
  clearCheckoutProxyTest('promotion')
}

function updatePixProtocol(value: string) {
  props.config.checkout.pix_protocol = value === 'reference'
    ? 'reference'
    : value === 'standalone'
      ? 'standalone'
      : 'enhanced'
  clearCheckoutProxyTest('checkout')
  clearCheckoutProxyTest('promotion')
}

const checkoutProxyFieldByStage = {
  checkout: 'checkout_proxy_url',
  promotion: 'promotion_proxy_url',
} as const
const checkoutProxyTesting = ref<CheckoutProxyTestStage | ''>('')
const checkoutProxyTestResults = ref<Partial<Record<CheckoutProxyTestStage, ProxySampleTestResult>>>({})

function clearCheckoutProxyTest(stage: CheckoutProxyTestStage) {
  const next = { ...checkoutProxyTestResults.value }
  delete next[stage]
  checkoutProxyTestResults.value = next
}

function checkoutProxyTestText(stage: CheckoutProxyTestStage) {
  const result = checkoutProxyTestResults.value[stage]
  if (!result) return ''
  const sample = result.sample_index > 0
    ? `抽样第 ${result.sample_index}/${result.sample_count} 条`
    : '抽样代理'
  if (result.ok) {
    const saved = result.normalized_changed ? '；已补全协议并自动保存，当前队列下一轮生效。' : '；协议已确认。'
    return `${sample}可用：${result.scheme.toUpperCase()}，HTTP ${result.status}，${result.latency_ms} ms${saved}`
  }
  return `${sample}不可用：${result.error || '代理测试失败'}`
}

async function testCheckoutProxy(stage: CheckoutProxyTestStage) {
  if (checkoutProxyTesting.value) return
  const field = checkoutProxyFieldByStage[stage]
  const urls = String(props.config.checkout[field] || '').trim()
  if (!urls) return
  checkoutProxyTesting.value = stage
  clearCheckoutProxyTest(stage)
  try {
    const response = await proxyApi.testSample(urls)
    const result = response.result
    if (result.ok && result.normalized_urls && result.normalized_changed) {
      props.config.checkout[field] = result.normalized_urls
    }
    checkoutProxyTestResults.value = {
      ...checkoutProxyTestResults.value,
      [stage]: result,
    }
  } catch (error: any) {
    checkoutProxyTestResults.value = {
      ...checkoutProxyTestResults.value,
      [stage]: {
        ok: false,
        status: 0,
        latency_ms: 0,
        error: error?.message || '代理抽样测试失败',
        scheme: '',
        sample_index: 0,
        sample_count: urls.split(/\r?\n/).filter(Boolean).length,
        attempts: [],
      },
    }
  } finally {
    checkoutProxyTesting.value = ''
  }
}

const sub2apiServerGroups = computed(() => {
  const options = [
    { label: '选择 Sub2API 连接', value: '' },
    ...sub2apiServers.value.map((server) => ({
      label: server.name || server.base_url || server.id,
      value: server.id,
    })),
  ]
  const currentId = String(props.config.sub2api_sync.server_id || '').trim()
  if (currentId && !options.some((option) => option.value === currentId)) {
    options.push({ label: `当前连接（不可用）· ${currentId}`, value: currentId })
  }
  return [{ options }]
})

const sub2apiRemoteGroupGroups = computed(() => {
  const options = [
    {
      label: '选择远端分组',
      value: '',
    },
    ...sub2apiRemoteGroups.value.map((group) => ({
      label: `${group.name || group.id}${group.account_count ? ` · ${group.active_account_count}/${group.account_count}` : ''}`,
      value: group.id,
    })),
  ]
  const currentId = String(props.config.sub2api_sync.group_id || '').trim()
  if (currentId && !options.some((option) => option.value === currentId)) {
    options.push({ label: `当前分组（未加载）· ${currentId}`, value: currentId })
  }
  return [{ options }]
})

const oauthDeliverySub2APIServerGroups = computed(() => {
  const options = [
    { label: '选择 Sub2API 连接', value: '' },
    ...sub2apiServers.value.map((server) => ({
      label: server.name || server.base_url || server.id,
      value: server.id,
    })),
  ]
  const currentId = String(props.config.grok.oauth_delivery.sub2api.server_id || '').trim()
  if (currentId && !options.some((option) => option.value === currentId)) {
    options.push({ label: `当前连接（不可用）· ${currentId}`, value: currentId })
  }
  return [{ options }]
})

const oauthDeliveryRemoteGroupGroups = computed(() => {
  const options = [
    { label: '选择 xAI 远程分组', value: '' },
    ...oauthDeliveryRemoteGroups.value.map((group) => ({
      label: `${group.name || group.id}${group.account_count ? ` · ${group.active_account_count}/${group.account_count}` : ''}`,
      value: group.id,
    })),
  ]
  const currentId = String(props.config.grok.oauth_delivery.sub2api.group_id || '').trim()
  if (currentId && !options.some((option) => option.value === currentId)) {
    options.push({ label: `当前分组（未加载）· ${currentId}`, value: currentId })
  }
  return [{ options }]
})

const oauthDeliveryCPAPoolGroups = computed(() => {
  const options = [
    { label: '选择 CPA 连接', value: '' },
    ...cpaPools.value.map((pool) => ({
      label: pool.name || pool.base_url || pool.id,
      value: pool.id,
    })),
  ]
  const currentId = String(props.config.grok.oauth_delivery.cpa.pool_id || '').trim()
  if (currentId && !options.some((option) => option.value === currentId)) {
    options.push({ label: `当前连接（不可用）· ${currentId}`, value: currentId })
  }
  return [{ options }]
})

const openAICPAPoolGroups = computed(() => {
  const options = [
    { label: '选择 CPA 连接', value: '' },
    ...cpaPools.value.map((pool) => ({
      label: pool.name || pool.base_url || pool.id,
      value: pool.id,
    })),
  ]
  const currentId = String(props.config.cpa_sync.pool_id || '').trim()
  if (currentId && !options.some((option) => option.value === currentId)) {
    options.push({ label: `当前连接（不可用）· ${currentId}`, value: currentId })
  }
  return [{ options }]
})

function selectValue(value: unknown) {
  return String(Array.isArray(value) ? value[0] || '' : value || '').trim()
}

async function loadSub2APIRemoteGroups(serverId: string) {
  const normalizedServerId = String(serverId || '').trim()
  const requestVersion = ++sub2apiGroupRequestVersion
  if (!normalizedServerId) {
    sub2apiRemoteGroups.value = []
    sub2apiGroupsLoading.value = false
    return
  }

  sub2apiGroupsLoading.value = true
  try {
    const response = await accountImportsApi.listSub2APIServerGroups(normalizedServerId)
    if (requestVersion !== sub2apiGroupRequestVersion) return
    sub2apiRemoteGroups.value = Array.isArray(response.groups) ? response.groups : []
    sub2apiLoadError.value = ''
  } catch (error: any) {
    if (requestVersion !== sub2apiGroupRequestVersion) return
    sub2apiRemoteGroups.value = []
    sub2apiLoadError.value = error?.message || '加载 Sub2API 远端分组失败'
  } finally {
    if (requestVersion === sub2apiGroupRequestVersion) sub2apiGroupsLoading.value = false
  }
}

async function refreshSub2APIConnections() {
  sub2apiLoading.value = true
  sub2apiLoadError.value = ''
  try {
    const response = await accountImportsApi.listSub2APIServers()
    sub2apiServers.value = Array.isArray(response.servers) ? response.servers : []
    const serverId = String(props.config.sub2api_sync.server_id || '').trim()
    if (serverId) await loadSub2APIRemoteGroups(serverId)
    else sub2apiRemoteGroups.value = []
  } catch (error: any) {
    sub2apiServers.value = []
    sub2apiRemoteGroups.value = []
    sub2apiLoadError.value = error?.message || '加载 Sub2API 连接失败'
  } finally {
    sub2apiLoading.value = false
  }
}

async function refreshOpenAICPAConnections() {
  openAICPALoading.value = true
  openAICPALoadError.value = ''
  try {
    const response = await accountImportsApi.listCPAPools()
    cpaPools.value = Array.isArray(response.pools) ? response.pools : []
  } catch (error: any) {
    cpaPools.value = []
    openAICPALoadError.value = error?.message || '加载 CPA 连接失败'
  } finally {
    openAICPALoading.value = false
  }
}

async function refreshOpenAIConnections() {
  await Promise.all([refreshSub2APIConnections(), refreshOpenAICPAConnections()])
}

async function loadOAuthDeliveryRemoteGroups(serverId: string) {
  const normalizedServerId = String(serverId || '').trim()
  const requestVersion = ++oauthDeliveryGroupRequestVersion
  if (!normalizedServerId) {
    oauthDeliveryRemoteGroups.value = []
    oauthDeliveryGroupsLoading.value = false
    return
  }
  oauthDeliveryGroupsLoading.value = true
  try {
    const response = await accountImportsApi.listSub2APIServerGroups(normalizedServerId)
    if (requestVersion !== oauthDeliveryGroupRequestVersion) return
    oauthDeliveryRemoteGroups.value = (Array.isArray(response.groups) ? response.groups : [])
      .filter((group) => ['', 'xai', 'grok'].includes(String(group.platform || '').trim().toLowerCase()))
    oauthDeliveryLoadError.value = ''
  } catch (error: any) {
    if (requestVersion !== oauthDeliveryGroupRequestVersion) return
    oauthDeliveryRemoteGroups.value = []
    oauthDeliveryLoadError.value = error?.message || '加载 Sub2API xAI 分组失败'
  } finally {
    if (requestVersion === oauthDeliveryGroupRequestVersion) oauthDeliveryGroupsLoading.value = false
  }
}

async function refreshOAuthDeliveryConnections() {
  oauthDeliveryLoading.value = true
  oauthDeliveryLoadError.value = ''
  const [sub2apiResult, cpaResult] = await Promise.allSettled([
    accountImportsApi.listSub2APIServers(),
    accountImportsApi.listCPAPools(),
  ])
  if (sub2apiResult.status === 'fulfilled') {
    sub2apiServers.value = Array.isArray(sub2apiResult.value.servers) ? sub2apiResult.value.servers : []
  } else {
    sub2apiServers.value = []
    oauthDeliveryLoadError.value = sub2apiResult.reason?.message || '加载 Sub2API 连接失败'
  }
  if (cpaResult.status === 'fulfilled') {
    cpaPools.value = Array.isArray(cpaResult.value.pools) ? cpaResult.value.pools : []
  } else {
    cpaPools.value = []
    oauthDeliveryLoadError.value ||= cpaResult.reason?.message || '加载 CPA 连接失败'
  }
  oauthDeliveryLoading.value = false
  const serverId = String(props.config.grok.oauth_delivery.sub2api.server_id || '').trim()
  if (serverId) await loadOAuthDeliveryRemoteGroups(serverId)
  else oauthDeliveryRemoteGroups.value = []
}

function updateOAuthDeliverySub2APIServer(value: unknown) {
  const delivery = props.config.grok.oauth_delivery.sub2api
  const serverId = selectValue(value)
  if (serverId === delivery.server_id) return
  delivery.server_id = serverId
  delivery.group_id = ''
  delivery.group_name = ''
}

function updateOAuthDeliveryGroupMode(value: unknown) {
  const delivery = props.config.grok.oauth_delivery.sub2api
  const mode = selectValue(value) === 'custom' ? 'custom' : 'existing'
  delivery.group_mode = mode
  if (mode === 'custom') delivery.group_id = ''
  else delivery.group_name = ''
}

function updateSub2APIServer(value: unknown) {
  const serverId = selectValue(value)
  if (serverId === props.config.sub2api_sync.server_id) return
  props.config.sub2api_sync.server_id = serverId
  props.config.sub2api_sync.group_id = ''
  props.config.sub2api_sync.group_name = ''
}

function updateSub2APIEnabled(value: unknown) {
  const enabled = Boolean(value)
  if (enabled === props.config.sub2api_sync.enabled) return
  props.config.sub2api_sync.enabled = enabled
}

function updateOpenAICPAEnabled(value: unknown) {
  props.config.cpa_sync.enabled = Boolean(value)
}

function updateOpenAICPAPool(value: unknown) {
  props.config.cpa_sync.pool_id = selectValue(value)
}

function updateSub2APIGroupMode(value: unknown) {
  const mode = selectValue(value) === 'custom' ? 'custom' : 'existing'
  if (mode === props.config.sub2api_sync.group_mode) return
  props.config.sub2api_sync.group_mode = mode
  if (mode === 'custom') props.config.sub2api_sync.group_id = ''
  else props.config.sub2api_sync.group_name = ''
}

function updateSub2APIRemoteGroup(value: unknown) {
  const groupId = selectValue(value)
  if (groupId === props.config.sub2api_sync.group_id) return
  props.config.sub2api_sync.group_id = groupId
}

function updateSub2APIGroupName(value: unknown) {
  const groupName = String(value || '').trim()
  if (groupName === props.config.sub2api_sync.group_name) return
  props.config.sub2api_sync.group_name = groupName
}

watch(
  () => String(props.config.target || '').trim().toLowerCase(),
  (target) => {
    if (
      target === 'openai'
      && (!sub2apiServers.value.length || !cpaPools.value.length)
      && !sub2apiLoading.value
      && !openAICPALoading.value
    ) {
      void refreshOpenAIConnections()
    }
    if (target === 'grok' && !oauthDeliveryLoading.value && (!sub2apiServers.value.length || !cpaPools.value.length)) {
      void refreshOAuthDeliveryConnections()
    }
  },
  { immediate: true },
)

watch(
  () => String(props.config.grok.oauth_delivery.sub2api.server_id || '').trim(),
  (serverId, previousServerId) => {
    if (String(props.config.target || '').trim().toLowerCase() !== 'grok') return
    if (!serverId) {
      oauthDeliveryRemoteGroups.value = []
      return
    }
    if (serverId !== previousServerId || !oauthDeliveryRemoteGroups.value.length) {
      void loadOAuthDeliveryRemoteGroups(serverId)
    }
  },
  { immediate: true },
)

watch(
  () => String(props.config.sub2api_sync.server_id || '').trim(),
  (serverId, previousServerId) => {
    if (String(props.config.target || '').trim().toLowerCase() !== 'openai') return
    if (!serverId) {
      sub2apiRemoteGroups.value = []
      return
    }
    if (serverId !== previousServerId || !sub2apiRemoteGroups.value.length) {
      void loadSub2APIRemoteGroups(serverId)
    }
  },
  { immediate: true },
)

type CheckoutProxyStage = {
  legend: string
  label: string
  description: string
}

type CheckoutProxyPlan = {
  flow: string
  checkout: CheckoutProxyStage
  promotion?: CheckoutProxyStage
  continuousRetryDescription?: string
}

const checkoutProxyPlan = computed<CheckoutProxyPlan>(() => {
  if (props.config.checkout.channel === 'pix') {
    if (props.config.checkout.pix_protocol === 'standalone') {
      return {
        flow: 'Pix · BR Checkout / Provider + VN Promotion（方案 3）',
        continuousRetryDescription: '每轮重新创建 Checkout，并刷新 BR、VN 两条 sticky 出口。',
        checkout: {
          legend: 'BR Checkout / Provider 代理',
          label: 'BR 代理 URL',
          description: '用于 Checkout、taxes、Stripe、Pix PaymentMethod、Approve 与轮询。',
        },
        promotion: {
          legend: 'VN Promotion 代理',
          label: 'VN Promotion 代理 URL',
          description: '用于 checkout/update 注入免费优惠；未单独配置时从 BR seed 派生 VN 地区。',
        },
      }
    }
    return {
      flow: 'Pix · BR 共享出口（Checkout / Promotion / Stripe / Approve / Poll）',
      continuousRetryDescription: '每轮重新创建 Checkout，并从 BR 代理池选择一条 sticky 出口贯穿全链路。',
      checkout: {
        legend: 'BR 共享代理',
        label: 'BR 共享代理 URL',
        description: '用于 Pix 全部阶段，同一轮始终复用同一 sticky 出口。',
      },
    }
  }
  return {
    flow: 'UPI · IN 共享出口（Checkout / Stripe / Approve / 最终提链）+ VN Promotion',
    continuousRetryDescription: '每轮重新创建 Checkout，并分别从 IN 共享代理池与 VN Promotion 代理池选择出口。',
    checkout: {
      legend: 'IN 共享代理',
      label: 'IN 共享代理 URL',
      description: '用于 Checkout、Stripe、UPI、Approve、轮询与最终跳转，全程复用同一 sticky 出口。',
    },
    promotion: {
      legend: 'VN Promotion 代理',
      label: 'VN Promotion 代理 URL',
      description: '用于 Checkout 更新与优惠应用。',
    },
  }
})
const usesPromotionProxy = computed(() => Boolean(checkoutProxyPlan.value.promotion))
const continuousRetryHint = computed(() => {
  const description = checkoutProxyPlan.value.continuousRetryDescription
  return description ? `${description} 提链并发数使用提链线程数，可在提链任务中单独结束。` : ''
})

const emit = defineEmits<{
  (e: 'update-target', value: string): void
  (e: 'update-proxy-mode', value: string): void
  (e: 'select-proxy-group', value: string): void
  (e: 'update-custom-proxy', value: string): void
}>()
</script>

<style scoped>
.register-task-settings {
  display: grid;
  gap: 16px;
}

.register-collapsible-section--collapsed :deep(.form-section__header) {
  align-items: center;
  margin-bottom: 0;
}

.register-form-grid {
  display: grid;
  gap: 12px;
}

.register-target-segmented {
  min-width: 0;
  margin: 0;
  border: 0;
  padding: 0;
}

.register-target-segmented:disabled {
  opacity: 0.58;
}

.checkout-channel-segmented {
  min-width: 0;
  margin: 0;
  border: 0;
  padding: 0;
}

.checkout-channel-segmented:disabled {
  opacity: 0.58;
}

.checkout-channel-segmented :deep(.ui-segmented) {
  width: min(100%, 280px);
}

.pix-protocol-segmented :deep(.ui-segmented) {
  width: min(100%, 460px);
}

.register-target-segmented :deep(.ui-segmented) {
  width: min(100%, 360px);
}

.register-target-segmented :deep(.ui-segmented-btn) {
  flex: 1 1 0;
  justify-content: center;
  min-height: 34px;
}

@media (min-width: 720px) {
  .register-form-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .register-field--full {
    grid-column: 1 / -1;
  }

  .register-form-grid--mail {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

.register-field {
  display: grid;
  min-width: 0;
  gap: 7px;
}

.register-label {
  font-size: 12px;
  color: hsl(var(--muted-foreground));
}

.checkout-proxy-field-header {
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.checkout-proxy-test-result {
  margin: 0;
  font-size: 12px;
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.checkout-proxy-test-result--success {
  color: hsl(var(--success-foreground, var(--foreground)));
}

.checkout-proxy-test-result--error {
  color: hsl(var(--destructive));
}

.checkout-channel-flow {
  margin: 0;
  color: hsl(var(--muted-foreground));
  font-size: 12px;
  line-height: 1.55;
}

.checkout-proxy-stage {
  display: grid;
  min-width: 0;
  margin: 0;
  border: 0;
  border-top: 1px solid hsl(var(--border));
  padding: 12px 0 0;
  gap: 10px;
}

.checkout-proxy-stage legend {
  padding: 0;
  font-size: 12px;
  font-weight: 600;
  color: hsl(var(--foreground));
}

.register-toggle {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: 10px;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  padding: 11px 12px;
  color: hsl(var(--foreground));
}

.register-toggle > span {
  display: grid;
  min-width: 0;
  gap: 3px;
}

.register-toggle strong {
  font-size: 12px;
  font-weight: 600;
}

.register-toggle small {
  font-size: 11px;
  line-height: 1.5;
  color: hsl(var(--muted-foreground));
}

.register-proxy-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: hsl(var(--muted-foreground));
}

.sub2api-sync-message {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: hsl(var(--muted-foreground));
}

.sub2api-sync-message--error {
  color: hsl(var(--destructive));
}

.sub2api-sync-message--success {
  color: hsl(var(--success-foreground, var(--foreground)));
}
</style>
