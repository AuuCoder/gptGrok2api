<template>
  <section class="checkout-task-table-panel" aria-label="提链任务列表">
    <div v-if="tasks.length === 0" class="checkout-task-table-panel__empty">
      <EmptyState plain title="暂无提链任务" description="注册账号后会在这里显示最终支付链接的提取进度。" />
    </div>

    <div v-else class="checkout-task-table-panel__body scrollbar-slim">
      <table class="checkout-task-table">
        <colgroup>
          <col class="checkout-task-table__account-column" />
          <col class="checkout-task-table__status-column" />
          <col class="checkout-task-table__link-column" />
          <col class="checkout-task-table__error-column" />
        </colgroup>
        <thead>
          <tr>
            <th>账号</th>
            <th>进度</th>
            <th>结果链接</th>
            <th>失败原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(task, rowIndex) in tasks" :key="taskKey(task, rowIndex)">
            <td>
              <p class="checkout-task-table__email" :title="accountText(task, rowIndex)">
                {{ accountText(task, rowIndex) }}
              </p>
              <p v-if="taskIndexText(task)" class="checkout-task-table__task-id">
                {{ taskIndexText(task) }}
              </p>
              <p v-if="channelText(task)" class="checkout-task-table__channel">
                {{ channelText(task) }}
              </p>
            </td>
            <td>
              <StateBadge :tone="statusTone(task)" size="xs" shape="rounded" :bordered="false">
                {{ statusText(task) }}
              </StateBadge>
              <p v-if="retryAtText(task)" class="checkout-task-table__retry-at">
                {{ retryAtText(task) }}
              </p>
              <p v-if="stageText(task)" class="checkout-task-table__stage">
                {{ stageText(task) }}
              </p>
              <p v-if="progressDetailText(task)" class="checkout-task-table__detail" :title="progressDetailText(task)">
                {{ progressDetailText(task) }}
              </p>
              <p v-if="updatedAtText(task.updated_at)" class="checkout-task-table__updated">
                {{ updatedAtText(task.updated_at) }}
              </p>
            </td>
            <td>
              <div v-if="safePaymentLink(task.payment_link)" class="checkout-task-table__link">
                <a
                  :href="safePaymentLink(task.payment_link)"
                  target="_blank"
                  rel="noreferrer"
                  :title="String(task.payment_link || '')"
                >
                  <span>{{ paymentLinkText(task.payment_link) }}</span>
                  <Icon icon="lucide:external-link" class="checkout-task-table__link-icon" />
                </a>
                <Button
                  size="xs"
                  variant="ghost"
                  icon-only
                  root-class="h-7 w-7 shrink-0"
                  title="复制链接"
                  @click="emit('copy-payment-link', safePaymentLink(task.payment_link))"
                >
                  <Icon icon="lucide:copy" class="h-3.5 w-3.5" />
                </Button>
              </div>
              <span v-else class="checkout-task-table__placeholder">-</span>
            </td>
            <td>
              <p v-if="errorText(task)" class="checkout-task-table__error" :title="errorText(task)">
                {{ errorText(task) }}
              </p>
              <span v-else class="checkout-task-table__placeholder">-</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { Icon } from '@iconify/vue'
import { Button, EmptyState } from 'nanocat-ui'

import StateBadge from '@/components/ai/StateBadge.vue'
import type { CheckoutTask } from '@/api/register'

const props = withDefaults(defineProps<{
  tasks?: CheckoutTask[]
}>(), {
  tasks: () => [],
})

const emit = defineEmits<{
  (e: 'copy-payment-link', value: string): void
}>()

type CheckoutTaskTone = 'success' | 'danger' | 'warning' | 'info' | 'muted'

const stageLabels: Record<string, string> = {
  account_saved: '账号已保存',
  checkout: '创建 Checkout',
  checkout_created: '创建 Checkout',
  checkout_update: '更新 Checkout',
  promotion: '更新优惠',
  stripe: '初始化 Stripe',
  stripe_init: '初始化 Stripe',
  stripe_bootstrap: 'IN Bootstrap 检查',
  stripe_provider: 'IN Provider 复核',
  stripe_elements: '创建 Elements 会话',
  stripe_token: '加载 Stripe 配置',
  billing: '提交 Billing',
  payment_method: '创建支付方式',
  confirm: '确认支付',
  confirm_retry: '二次确认支付',
  approve: '等待授权',
  approved: '授权完成',
  poll: '轮询结果链接',
  extracting: '提取最终链接',
  extract: '提取最终链接',
  final_validate: '校验最终链接',
  queued: '排队',
  running: '提链中',
  retrying: '重试等待',
  completed: '提链完成',
  complete: '提链完成',
  success: '提链完成',
  cancelled: '已停止',
  canceled: '已停止',
  failed: '提链失败',
}

function normalized(value: unknown) {
  return String(value || '').trim().toLowerCase().replaceAll('-', '_').replaceAll(' ', '_')
}

function taskKey(task: CheckoutTask, rowIndex: number) {
  const taskId = String(task.task_id || '').trim()
  if (taskId) return taskId
  return `${task.index ?? rowIndex}-${task.email || 'checkout'}`
}

function taskIndexText(task: CheckoutTask) {
  const index = Number(task.index)
  return Number.isFinite(index) && index > 0 ? `任务 ${index}` : ''
}

function accountText(task: CheckoutTask, rowIndex: number) {
  const email = String(task.email || '').trim()
  return email || `任务 ${Number(task.index) || rowIndex + 1}`
}

function taskStatus(task: CheckoutTask) {
  return normalized(task.status || task.stage)
}

function taskAttempt(task: CheckoutTask) {
  const attempt = Number(task.attempt)
  return Number.isFinite(attempt) && attempt > 0 ? Math.floor(attempt) : 0
}

function completionText() {
  return '提链完成'
}

function statusTone(task: CheckoutTask): CheckoutTaskTone {
  const status = taskStatus(task)
  if (safePaymentLink(task.payment_link) || ['success', 'succeeded', 'completed', 'complete', 'done'].includes(status)) return 'success'
  if (['failed', 'error'].includes(status)) return 'danger'
  if (['cancelled', 'canceled', 'stopped'].includes(status)) return 'muted'
  if (['retrying', 'retry_waiting', 'retry_wait', 'retry'].includes(status)) return 'warning'
  if (['waiting', 'queued', 'pending'].includes(status)) return 'warning'
  if (['running', 'processing', 'extracting', 'started'].includes(status)) return 'info'
  return 'muted'
}

function statusText(task: CheckoutTask) {
  const status = taskStatus(task)
  const attempt = taskAttempt(task)
  if (safePaymentLink(task.payment_link) || ['success', 'succeeded', 'completed', 'complete', 'done'].includes(status)) return completionText()
  if (['cancelled', 'canceled', 'stopped'].includes(status)) return '已停止'
  if (['failed', 'error'].includes(status)) return '提链失败'
  if (['retrying', 'retry_waiting', 'retry_wait', 'retry'].includes(status)) return '重试等待'
  if (['waiting', 'queued', 'pending'].includes(status)) return '排队'
  if (['running', 'processing', 'extracting', 'started'].includes(status)) {
    return attempt > 0 ? `提链中（第 ${attempt} 轮）` : '提链中'
  }
  return attempt > 0 ? `提链中（第 ${attempt} 轮）` : '排队'
}

function stageText(task: CheckoutTask) {
  const stage = normalized(task.stage)
  if (!stage) return ''
  if (stage === taskStatus(task)) return ''
  return stageLabels[stage] || String(task.stage || '').trim()
}

function progressDetailText(task: CheckoutTask) {
  return String(task.progress_detail || '').replace(/\s+/g, ' ').trim()
}

function updatedAtText(value: unknown) {
  const text = String(value || '').trim()
  if (!text) return ''
  const date = new Date(text)
  if (Number.isNaN(date.getTime())) return text
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function retryAtText(task: CheckoutTask) {
  const status = taskStatus(task)
  if (!['retrying', 'retry_waiting', 'retry_wait', 'retry'].includes(status)) return ''
  const time = updatedAtText(task.next_retry_at)
  return time ? `下次重试 ${time}` : ''
}

function channelText(task: CheckoutTask) {
  const channel = normalized(task.channel)
  if (channel === 'pix') return 'Pix'
  if (channel === 'upi') return 'UPI'
  return ''
}

function safePaymentLink(value: unknown) {
  const text = String(value || '').trim()
  if (!text) return ''
  try {
    const url = new URL(text)
    return ['http:', 'https:'].includes(url.protocol) ? url.toString() : ''
  } catch {
    return ''
  }
}

function paymentLinkText(value: unknown) {
  const link = safePaymentLink(value)
  if (!link) return ''
  try {
    const url = new URL(link)
    return `${url.hostname}${url.pathname}`.replace(/\/$/, '') || url.hostname
  } catch {
    return link
  }
}

function errorText(task: CheckoutTask) {
  const raw = String(task.error_short || '').replace(/\s+/g, ' ').trim()
  if (!raw) return ''
  if (/^第\s*\d+\s*轮提链中$/.test(raw)) return ''
  const lower = raw.toLowerCase()
  if (lower.includes('stripe') && (lower.includes('poll') || lower.includes('轮询')) && (lower.includes('timeout') || lower.includes('timed out'))) return 'Stripe 轮询超时'
  if (lower.includes('proxy') || lower.includes('connect aborted')) return '代理连接失败'
  if (lower.includes('timeout') || lower.includes('timed out')) return '请求超时'
  if (lower.includes('rate limit') || lower.includes('429')) return '上游限流'
  if (lower.includes('permission') || lower.includes('forbidden') || lower.includes('403')) return '上游拒绝访问'
  if (lower.includes('unable to serve your request')) return '上游暂时拒绝请求'
  if (lower.includes('final') && lower.includes('link')) return '未取得最终支付链接'
  if (/https?:\/\//i.test(raw) || /\b(?:\d{1,3}\.){3}\d{1,3}\b/.test(raw)) return '提链请求失败，请稍后重试'
  return raw.length > 42 ? `${raw.slice(0, 41)}...` : raw
}
</script>

<style scoped>
.checkout-task-table-panel {
  display: flex;
  height: clamp(24rem, 52vh, 38rem);
  height: clamp(24rem, 52dvh, 38rem);
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  background: hsl(var(--background));
}

.checkout-task-table-panel__empty {
  display: flex;
  min-height: 0;
  flex: 1;
  align-items: center;
  justify-content: center;
  padding: 16px;
}

.checkout-task-table-panel__body {
  min-height: 0;
  flex: 1;
  overflow: auto;
}

.checkout-task-table {
  width: 100%;
  min-width: 48rem;
  table-layout: fixed;
  border-collapse: collapse;
  font-size: 12px;
}

.checkout-task-table__account-column {
  width: 11rem;
}

.checkout-task-table__status-column {
  width: 45%;
}

.checkout-task-table__link-column {
  width: 7rem;
}

.checkout-task-table__error-column {
  width: 9rem;
}

.checkout-task-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: hsl(var(--muted) / 0.35);
  padding: 9px 12px;
  color: hsl(var(--muted-foreground));
  font-weight: 500;
  text-align: left;
}

.checkout-task-table td {
  min-width: 0;
  border-top: 1px solid hsl(var(--border));
  padding: 10px 12px;
  vertical-align: middle;
}

.checkout-task-table tbody tr:hover td {
  background: hsl(var(--muted) / 0.17);
}

.checkout-task-table__email,
.checkout-task-table__task-id,
.checkout-task-table__channel,
.checkout-task-table__stage,
.checkout-task-table__detail,
.checkout-task-table__retry-at,
.checkout-task-table__updated,
.checkout-task-table__error {
  margin: 0;
}

.checkout-task-table__email {
  overflow: hidden;
  color: hsl(var(--foreground));
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.checkout-task-table__task-id,
.checkout-task-table__channel,
.checkout-task-table__stage,
.checkout-task-table__detail,
.checkout-task-table__retry-at,
.checkout-task-table__updated {
  margin-top: 4px;
  color: hsl(var(--muted-foreground));
  font-size: 11px;
  line-height: 1.35;
}

.checkout-task-table__detail {
  color: hsl(var(--foreground) / 0.82);
  line-height: 1.45;
  overflow-wrap: anywhere;
  white-space: normal;
}

.checkout-task-table__task-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
}

.checkout-task-table__retry-at {
  color: hsl(var(--warning-foreground, var(--muted-foreground)));
}

.checkout-task-table__link {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 4px;
}

.checkout-task-table__link a {
  display: flex;
  min-width: 0;
  flex: 1;
  align-items: center;
  gap: 4px;
  overflow: hidden;
  color: hsl(var(--primary));
  text-decoration: none;
}

.checkout-task-table__link a:hover,
.checkout-task-table__link a:focus-visible {
  text-decoration: underline;
}

.checkout-task-table__link a span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.checkout-task-table__link-icon {
  width: 13px;
  height: 13px;
  flex: 0 0 auto;
}

.checkout-task-table__error {
  display: -webkit-box;
  overflow: hidden;
  color: hsl(var(--muted-foreground));
  line-height: 1.45;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.checkout-task-table__placeholder {
  color: hsl(var(--muted-foreground));
}

@media (max-width: 1279px) {
  .checkout-task-table-panel {
    height: clamp(22rem, 48vh, 34rem);
    height: clamp(22rem, 48dvh, 34rem);
  }
}

@media (max-width: 640px) {
  .checkout-task-table-panel {
    height: auto;
    min-height: 0;
  }

  .checkout-task-table-panel__empty {
    min-height: 12rem;
  }

  .checkout-task-table-panel__body {
    max-height: min(46vh, 26rem);
    max-height: min(46dvh, 26rem);
  }
}
</style>
