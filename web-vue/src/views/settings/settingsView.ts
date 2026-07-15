import { prepareSettingsForSave } from '@/api/settings'
import type { BackupState } from '@/api/settings'
import type { ProxyRuntimeStatus, Settings } from '@/types/api'

export type SettingsSelectOption = {
  label: string
  value: string
}

export type SettingsSummaryItem = {
  label: string
  value: string
}

export type SettingsApiDocItem = {
  title: string
  method: string
  path: string
  description: string
  example: string
}

export type SettingsImageErrorMessageKey = keyof Settings['image_error_messages']

export type SettingsImageErrorMessageField = {
  key: SettingsImageErrorMessageKey
  label: string
  placeholder: string
  help?: string
}

export type SettingsImageErrorMessageGroup = {
  key: string
  title: string
  fields: SettingsImageErrorMessageField[]
}

export type SettingsBackupIncludeKey =
  | 'config'
  | 'register'
  | 'cpa'
  | 'sub2api'
  | 'logs'
  | 'dashboard_metrics'
  | 'image_tasks'
  | 'accounts_snapshot'
  | 'auth_keys_snapshot'
  | 'images'

export type SettingsBackupIncludeOption = {
  value: SettingsBackupIncludeKey
  label: string
}

export type NumberSettingOptions = {
  integer?: boolean
  min?: number
  max?: number
  fallback?: number
}

const dateTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
})

export const settingsTabs: SettingsSelectOption[] = [
  { value: 'basic', label: '基础配置' },
  { value: 'image-errors', label: '图片错误' },
  { value: 'storage', label: '图片存储与审核' },
  { value: 'prompts', label: '提示词源' },
  { value: 'backup', label: 'R2 备份' },
  { value: 'keys', label: '用户密钥' },
  { value: 'api-docs', label: '接口接入' },
  { value: 'canvas', label: '画布入口' },
  { value: 'cpa', label: 'CPA' },
  { value: 'sub2api', label: 'Sub2API' },
]

export const imageErrorMessageGroups: SettingsImageErrorMessageGroup[] = [
  {
    key: 'account',
    title: '账号与配置',
    fields: [
      {
        key: 'quota',
        label: '额度耗尽',
        placeholder: '图片账号额度已用完，请稍后再试或联系管理员。',
      },
      {
        key: 'no_account',
        label: '无可用账号',
        placeholder: '当前图片账号暂不可用，可能是账号池、并发或上游波动，请稍后重试。',
      },
      {
        key: 'local_busy',
        label: '本地繁忙',
        placeholder: '当前没有可用的图片账号或账号并发已满，请稍后重试。',
      },
      {
        key: 'unsupported_model',
        label: '模型不支持',
        placeholder: '当前模型不支持图片生成，请检查 model 参数。',
      },
      {
        key: 'token_invalid',
        label: '账号状态异常',
        placeholder: '图片生成账号状态异常，请稍后重试。',
      },
    ],
  },
  {
    key: 'upstream',
    title: '上游链路',
    fields: [
      {
        key: 'poll_timeout',
        label: '轮询超时',
        placeholder: '图片任务暂未返回结果，可能仍在排队或上游处理较慢，请重试。',
      },
      {
        key: 'stream_interrupted',
        label: '上游断流',
        placeholder: '图片生成连接中断，可能是上游服务繁忙或网络波动，请重试。',
      },
      {
        key: 'connection_failed',
        label: '连接失败',
        placeholder: '连接上游图片服务失败，可能是网络或代理波动，请重试。',
      },
      {
        key: 'connection_timeout',
        label: '连接超时',
        placeholder: '连接上游图片服务超时，请稍后重试。',
      },
    ],
  },
  {
    key: 'result',
    title: '结果语义',
    fields: [
      {
        key: 'text_reply',
        label: '返回文本但无图',
        placeholder: '上游返回了文本说明，未生成图片。请调整提示词或重试。',
        help: '可使用 {text} 指定上游文本插入位置；不写占位符时会自动追加到下一行。',
      },
    ],
  },
  {
    key: 'fallback',
    title: '兜底',
    fields: [
      {
        key: 'fallback',
        label: '兜底错误',
        placeholder: '图片生成请求失败，请稍后重试。',
      },
    ],
  },
]

export const imageErrorMessageFields: SettingsImageErrorMessageField[] = imageErrorMessageGroups.flatMap(
  (group) => group.fields,
)

export const logLevelOptions = ['debug', 'info', 'warning', 'error'] as const

export const backupIncludeOptions: SettingsBackupIncludeOption[] = [
  { value: 'config', label: '系统配置' },
  { value: 'register', label: '注册配置' },
  { value: 'cpa', label: 'CPA 配置' },
  { value: 'sub2api', label: 'Sub2API 配置' },
  { value: 'logs', label: '调度与调用日志' },
  { value: 'dashboard_metrics', label: '概览统计' },
  { value: 'image_tasks', label: '图片任务记录' },
  { value: 'accounts_snapshot', label: '账号快照' },
  { value: 'auth_keys_snapshot', label: '用户密钥快照' },
  { value: 'images', label: '图片文件目录' },
]

export const imageStorageModeOptions: SettingsSelectOption[] = [
  { label: '仅本地', value: 'local' },
  { label: '仅 WebDAV', value: 'webdav' },
  { label: '本地 + WebDAV', value: 'both' },
]

export const proxyRuntimeEgressOptions: SettingsSelectOption[] = [
  { label: '直连', value: 'direct' },
  { label: '单代理', value: 'single_proxy' },
]

export const proxyClearanceModeOptions: SettingsSelectOption[] = [
  { label: '关闭', value: 'none' },
  { label: 'FlareSolverr', value: 'flaresolverr' },
  { label: '手动 Cookie', value: 'manual' },
]

export function backupStatusText(state: BackupState | null | undefined) {
  if (!state) return '未加载'
  if (state.running) return '备份中'
  if (state.last_status === 'success') return '最近成功'
  if (state.last_status === 'error') return '最近失败'
  return state.last_status || '未执行'
}

export function buildProxyRuntimeSummaryItems(status: ProxyRuntimeStatus | null | undefined): SettingsSummaryItem[] {
  return [
    { label: '运行时', value: status ? (status.enabled ? '已启用' : '关闭') : '-' },
    { label: '出站方式', value: status ? (status.egress_mode === 'single_proxy' ? '单代理' : '直连') : '-' },
    { label: '代理', value: status ? (status.has_proxy ? '已配置' : '未配置') : '-' },
    { label: '清障', value: status ? (status.clearance_enabled ? `已启用 / ${status.clearance_mode}` : '关闭') : '-' },
    { label: '缓存', value: status ? (status.has_clearance_bundle ? '已有 clearance' : '暂无缓存') : '-' },
  ]
}

export function formatBytes(value: unknown) {
  const bytes = Number(value) || 0
  if (bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let size = bytes
  let unitIndex = 0
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024
    unitIndex += 1
  }
  return `${size.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`
}

export function formatDateTime(value: unknown) {
  const raw = String(value || '').trim()
  if (!raw) return '-'
  const parsed = new Date(raw)
  if (Number.isNaN(parsed.getTime())) return raw
  return dateTimeFormatter.format(parsed)
}

export function normalizeNumberSetting(value: unknown, options: NumberSettingOptions = {}): number {
  const fallback = options.fallback ?? 0
  const min = options.min ?? 0
  const parsed = Number(value)
  const finite = Number.isFinite(parsed) ? parsed : fallback
  const bounded = Math.max(min, finite)
  const clamped = typeof options.max === 'number' ? Math.min(options.max, bounded) : bounded
  return options.integer ? Math.round(clamped) : clamped
}

export function settingsFingerprint(value: Settings | null | undefined): string {
  return value ? JSON.stringify(prepareSettingsForSave(value)) : ''
}

export function buildApiDocItems(
  serviceBaseUrl: string,
  currentApiKey: string,
  models: { chat?: string; image?: string; imageEdit?: string } = {},
): SettingsApiDocItem[] {
  const cleanServiceBaseUrl = String(serviceBaseUrl || '').replace(/\/$/, '')
  const openAIBaseUrl = `${cleanServiceBaseUrl}/v1`
  const chatModel = models.chat || 'gpt-5-mini'
  const imageModel = models.image || 'gpt-image-2'
  const imageEditModel = models.imageEdit || imageModel
  return [
    {
      title: '模型列表',
      method: 'GET',
      path: '/v1/models',
      description: '返回当前 GPT 与 Grok 可用模型。',
      example: `curl ${openAIBaseUrl}/models \\\n  -H "Authorization: Bearer ${currentApiKey}"`,
    },
    {
      title: '聊天补全',
      method: 'POST',
      path: '/v1/chat/completions',
      description: 'GPT 与 Grok 共用的 OpenAI 兼容聊天补全接口。',
      example: `curl ${openAIBaseUrl}/chat/completions \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer ${currentApiKey}" \\\n  -d '{"model":"${chatModel}","messages":[{"role":"user","content":"你好"}]}'`,
    },
    {
      title: 'Responses',
      method: 'POST',
      path: '/v1/responses',
      description: 'GPT 与 Grok 共用的 Responses 兼容入口。',
      example: `curl ${openAIBaseUrl}/responses \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer ${currentApiKey}" \\\n  -d '{"model":"${chatModel}","input":"介绍一座未来城市"}'`,
    },
    {
      title: 'Messages',
      method: 'POST',
      path: '/v1/messages',
      description: 'GPT 与 Grok 共用的 Anthropic Messages 兼容入口。',
      example: `curl ${openAIBaseUrl}/messages \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer ${currentApiKey}" \\\n  -d '{"model":"${chatModel}","max_tokens":1024,"messages":[{"role":"user","content":"你好"}]}'`,
    },
    {
      title: '联网搜索',
      method: 'POST',
      path: '/v1/search',
      description: '本地搜索入口，返回 answer 与 sources。',
      example: `curl ${openAIBaseUrl}/search \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer ${currentApiKey}" \\\n  -d '{"prompt":"今天的 AI 新闻"}'`,
    },
    {
      title: '图片生成',
      method: 'POST',
      path: '/v1/images/generations',
      description: 'GPT 与 Grok 共用的图片生成入口，按 model 自动路由供应商。',
      example: `curl ${openAIBaseUrl}/images/generations \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer ${currentApiKey}" \\\n  -d '{"model":"${imageModel}","prompt":"一张极简产品海报","n":1}'`,
    },
    {
      title: '图片编辑',
      method: 'POST',
      path: '/v1/images/edits',
      description: 'GPT 与 Grok 共用的图片编辑入口，按 model 自动路由供应商。',
      example: `curl ${openAIBaseUrl}/images/edits \\\n  -H "Authorization: Bearer ${currentApiKey}" \\\n  -F "model=${imageEditModel}" \\\n  -F "prompt=改成赛博朋克夜景" \\\n  -F "image=@./input.png"`,
    },
    {
      title: '创建可编辑文件任务',
      method: 'POST',
      path: '/v1/editable-file-tasks',
      description: '统一创建 PPT/PSD 文件任务，kind 可填 ppt 或 psd。',
      example: `curl ${openAIBaseUrl}/editable-file-tasks \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer ${currentApiKey}" \\\n  -d '{"kind":"ppt","prompt":"做一份产品发布会 PPT"}'`,
    },
    {
      title: '查询可编辑文件任务',
      method: 'GET',
      path: '/v1/editable-file-tasks?ids={taskId1,taskId2}',
      description: '按任务 ID 查询 PPT/PSD 文件生成状态。',
      example: `curl "${openAIBaseUrl}/editable-file-tasks?ids=task_1,task_2" \\\n  -H "Authorization: Bearer ${currentApiKey}"`,
    },
    {
      title: 'PPT 生成任务',
      method: 'POST',
      path: '/v1/ppt/generations',
      description: '直接创建 PPT 生成任务，返回任务 ID 后再查询状态。',
      example: `curl ${openAIBaseUrl}/ppt/generations \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer ${currentApiKey}" \\\n  -d '{"prompt":"生成一份市场分析 PPT"}'`,
    },
    {
      title: 'PSD 生成任务',
      method: 'POST',
      path: '/v1/psd/generations',
      description: '直接创建 PSD 生成任务，返回任务 ID 后再查询状态。',
      example: `curl ${openAIBaseUrl}/psd/generations \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer ${currentApiKey}" \\\n  -d '{"prompt":"生成一张电商海报 PSD"}'`,
    },
    {
      title: '文件下载',
      method: 'GET',
      path: '/files/{file_path}',
      description: '下载 PPT/PSD 任务生成的公开文件。',
      example: `curl ${cleanServiceBaseUrl}/files/{file_path}`,
    },
  ]
}
