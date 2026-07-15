<template>
  <div class="debug-center space-y-5">
    <PagePanel class="debug-center__panel">
      <PanelHeader title="调试中心">
        <template #copy>
          <p class="mt-1 text-xs text-muted-foreground">搜索、Skills、PPT、PSD、对话这些旧调试工具集中在这里。</p>
        </template>
      </PanelHeader>

      <ConsoleSegmentedTabs v-model="activeTab" :options="tabOptions" aria-label="调试工具" />
    </PagePanel>

    <PagePanel v-if="activeTab === 'search'" class="debug-center__panel">
      <PanelHeader title="搜索">
        <template #actions>
          <Button size="sm" variant="primary" :disabled="searchLoading || !searchPrompt.trim()" @click="runSearch">
            {{ searchLoading ? '搜索中...' : '开始搜索' }}
          </Button>
        </template>
      </PanelHeader>
      <textarea
        v-model.trim="searchPrompt"
        class="debug-textarea"
        rows="4"
        placeholder="输入要搜索的问题"
      ></textarea>
      <div v-if="searchError" class="debug-error">{{ searchError }}</div>
      <div v-if="searchResult" class="debug-result-grid">
        <div class="debug-result-card">
          <div class="debug-result-meta">
            <StateBadge tone="success" shape="rounded" :bordered="false">{{ searchResult.status || 'done' }}</StateBadge>
            <span>{{ searchElapsedLabel }}</span>
            <span>{{ searchResult.sources?.length || 0 }} sources</span>
          </div>
          <div class="debug-answer">{{ searchResult.answer || '-' }}</div>
        </div>
        <div v-if="searchResult.sources?.length" class="debug-result-card">
          <p class="ui-section-kicker">来源</p>
          <a
            v-for="(source, index) in searchResult.sources"
            :key="`${source.url || index}`"
            class="debug-source-link"
            :href="source.url"
            target="_blank"
            rel="noopener noreferrer"
          >
            <span class="truncate">{{ source.title || source.url || 'source' }}</span>
            <span class="truncate text-muted-foreground">{{ source.url }}</span>
          </a>
        </div>
      </div>
    </PagePanel>

    <PagePanel v-else-if="activeTab === 'skills'" class="debug-center__panel">
      <PanelHeader title="Skills 搜索">
        <template #actions>
          <Button size="sm" variant="outline" @click="copyText(skillInstallPromptZh)">复制中文安装指令</Button>
          <Button size="sm" variant="outline" @click="copyText(skillInstallPromptEn)">复制英文安装指令</Button>
        </template>
      </PanelHeader>
      <div class="grid gap-4 lg:grid-cols-2">
        <div>
          <p class="ui-section-kicker">中文安装指令</p>
          <CodeBlock :content="skillInstallPromptZh" />
        </div>
        <div>
          <p class="ui-section-kicker">English install prompt</p>
          <CodeBlock :content="skillInstallPromptEn" />
        </div>
      </div>
    </PagePanel>

    <PagePanel v-else-if="activeTab === 'ppt' || activeTab === 'psd'" class="debug-center__panel">
      <PanelHeader :title="activeTab === 'ppt' ? 'PPT 生成' : 'PSD 生成'">
        <template #actions>
          <Button size="sm" variant="outline" :disabled="editableFetching" @click="refreshEditableTasks">
            {{ editableFetching ? '刷新中...' : '刷新任务' }}
          </Button>
          <Button size="sm" variant="primary" :disabled="editableSubmitting || !editablePrompt.trim()" @click="submitEditableTask">
            {{ editableSubmitting ? '提交中...' : '提交任务' }}
          </Button>
        </template>
      </PanelHeader>
      <div class="grid gap-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <div class="space-y-3">
          <textarea
            v-model.trim="editablePrompt"
            class="debug-textarea"
            rows="9"
            placeholder="输入生成要求"
          ></textarea>
          <textarea
            v-model.trim="editableImages"
            class="debug-textarea debug-textarea--small"
            rows="4"
            placeholder="可选：每行一个 base64/data:image 参考图"
          ></textarea>
          <div v-if="editableError" class="debug-error">{{ editableError }}</div>
        </div>
        <div class="debug-task-list">
          <div v-if="editableTasks.length === 0" class="debug-empty">暂无任务</div>
          <article
            v-for="task in editableTasks"
            :key="editableTaskId(task)"
            class="debug-task-card"
          >
            <div class="flex flex-wrap items-center gap-2">
              <StateBadge :tone="editableStatusTone(task.status)" shape="rounded" :bordered="false">
                {{ editableStatusLabel(task.status) }}
              </StateBadge>
              <span class="font-mono text-[11px] text-muted-foreground">{{ editableTaskId(task) }}</span>
            </div>
            <p class="mt-2 text-sm text-foreground">{{ task.prompt_preview || '-' }}</p>
            <p v-if="task.error" class="mt-2 text-xs text-rose-600">{{ task.error }}</p>
            <div v-if="task.result?.primary_url || task.result?.zip_url" class="mt-3 flex flex-wrap gap-2">
              <Button v-if="task.result?.primary_url" size="xs" variant="outline" @click="openUrl(task.result.primary_url)">打开结果</Button>
              <Button v-if="task.result?.zip_url" size="xs" variant="outline" @click="openUrl(task.result.zip_url)">下载 ZIP</Button>
            </div>
          </article>
        </div>
      </div>
    </PagePanel>

    <PagePanel v-else class="debug-center__panel">
      <PanelHeader title="对话">
        <template #actions>
          <Button size="sm" variant="outline" @click="clearChat">清空</Button>
          <Button size="sm" variant="primary" :disabled="chatLoading || !chatInput.trim()" @click="sendChat">
            {{ chatLoading ? '发送中...' : '发送' }}
          </Button>
        </template>
      </PanelHeader>
      <div class="grid gap-4 lg:grid-cols-[22rem_minmax(0,1fr)]">
        <div class="space-y-3">
          <GroupedSelectMenu
            v-model="chatModel"
            :groups="chatModelGroups"
            placeholder="选择 GPT / Grok 模型"
            aria-label="对话模型"
            block
          />
          <select v-model="chatReasoningEffort" class="debug-select" aria-label="思考强度">
            <option v-for="option in reasoningEffortOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
          <textarea v-model.trim="chatInput" class="debug-textarea" rows="8" placeholder="输入消息"></textarea>
          <div v-if="chatError" class="debug-error">{{ chatError }}</div>
          <CodeBlock :content="chatRawJson" />
        </div>
        <div class="debug-chat-box">
          <div v-if="chatMessages.length === 0" class="debug-empty">暂无对话消息</div>
          <article v-for="(message, index) in chatMessages" :key="`${message.role}-${index}`" class="debug-chat-message">
            <p class="ui-section-kicker">{{ message.role }}</p>
            <p class="whitespace-pre-wrap text-sm leading-7 text-foreground">{{ message.content }}</p>
          </article>
        </div>
      </div>
    </PagePanel>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Button } from 'nanocat-ui'
import CodeBlock from '@/components/ai/CodeBlock.vue'
import ConsoleSegmentedTabs from '@/components/ai/ConsoleSegmentedTabs.vue'
import PagePanel from '@/components/ai/PagePanel.vue'
import PanelHeader from '@/components/ai/PanelHeader.vue'
import StateBadge from '@/components/ai/StateBadge.vue'
import GroupedSelectMenu from '@/components/ui/GroupedSelectMenu.vue'
import { debugApi } from '@/api/debug'
import type { DebugChatCompletion, DebugChatMessage, DebugEditableFileTask, DebugEditableKind, DebugSearchResult } from '@/api/debug'
import { getAuthToken } from '@/api/client'
import { useToast } from '@/composables/useToast'
import { useModelCatalog } from '@/composables/useModelCatalog'
import { useSettingsStore } from '@/stores/settings'

type DebugTab = 'search' | 'skills' | 'ppt' | 'psd' | 'chat'

const toast = useToast()
const settingsStore = useSettingsStore()
const { chatModels, loadModelCatalog } = useModelCatalog(() => settingsStore.settings)
const activeTab = ref<DebugTab>('search')
const tabOptions = [
  { label: '搜索', value: 'search' },
  { label: 'Skills 搜索', value: 'skills' },
  { label: 'PPT 生成', value: 'ppt' },
  { label: 'PSD 生成', value: 'psd' },
  { label: '对话', value: 'chat' },
]

const searchPrompt = ref('帮我搜索 GPTGrok2API 相关资料')
const searchResult = ref<DebugSearchResult | null>(null)
const searchError = ref('')
const searchLoading = ref(false)
const searchElapsedMs = ref(0)
const searchElapsedLabel = computed(() => `${(searchElapsedMs.value / 1000).toFixed(2)}s`)

const chatModel = ref('auto')
const chatModelGroups = computed(() => {
  const models = Array.from(new Set(['auto', ...chatModels.value]))
  const groups = [
    {
      label: '自动 / GPT',
      options: models
        .filter(model => model === 'auto' || model.startsWith('gpt-'))
        .map(model => ({ label: model === 'auto' ? '自动模型' : model, value: model })),
    },
    {
      label: 'Grok',
      options: models
        .filter(model => model.startsWith('grok-'))
        .map(model => ({ label: model, value: model })),
    },
    {
      label: '其他',
      options: models
        .filter(model => model !== 'auto' && !model.startsWith('gpt-') && !model.startsWith('grok-'))
        .map(model => ({ label: model, value: model })),
    },
  ]
  return groups.filter(group => group.options.length > 0)
})
const chatReasoningEffort = ref('')
const chatInput = ref('你好，先记住我的项目叫 GPTGrok2API。')
const chatMessages = ref<DebugChatMessage[]>([])
const chatRaw = ref<DebugChatCompletion | null>(null)
const chatLoading = ref(false)
const chatError = ref('')
const chatRawJson = computed(() => JSON.stringify(chatRaw.value || { messages: [] }, null, 2))
const reasoningEffortOptions = [
  { label: '默认思考', value: '' },
  { label: '低', value: 'low' },
  { label: '中', value: 'medium' },
  { label: '高', value: 'high' },
  { label: '超高', value: 'extended' },
]

const editablePrompt = ref('')
const editableImages = ref('')
const editableTasks = ref<DebugEditableFileTask[]>([])
const editableSubmitting = ref(false)
const editableFetching = ref(false)
const editableError = ref('')

const apiBaseUrl = computed(() => {
  const raw = import.meta.env.VITE_API_URL || window.location.origin
  return raw.replace(/\/$/, '')
})
const authKey = computed(() => getAuthToken())
const skillZh = computed(() => `---
name: gptgrok2api-search
description: 当用户需要联网搜索、查询最新信息、核实事实或需要来源链接时，调用本地 GPTGrok2API 搜索接口。
---

# GPTGrok2API 搜索

当用户要求联网搜索、查询最新信息、核实资料、查新闻、查价格、查文档更新或需要来源链接时，使用这个 skill。

## 接口

POST ${apiBaseUrl.value}/v1/search

Headers:

Authorization: Bearer ${authKey.value}
Content-Type: application/json

Body:

{
  "prompt": "<用户要搜索的问题>"
}

## 返回处理

- 使用接口返回的 \`answer\` 作为主要回答。
- 如果有 \`sources\`，在回答里附上来源链接。
- 如果接口报错，简要说明错误并询问是否重试。`)

const skillEn = computed(() => `---
name: gptgrok2api-search
description: Use when current web search is needed through this GPTGrok2API server. Call the configured HTTP search endpoint with a prompt and return the answer with source URLs.
---

# GPTGrok2API Search

Use this skill when the user asks for current web search, online lookup, recent information, or source-backed answers.

## Request

POST ${apiBaseUrl.value}/v1/search

Headers:

Authorization: Bearer ${authKey.value}
Content-Type: application/json

JSON body:

{
  "prompt": "<search question>"
}

## Response handling

- Use \`answer\` as the main response.
- Include source URLs from \`sources\` when available.
- If the endpoint returns an error, summarize the error and ask whether to retry.`)

const skillInstallPromptZh = computed(() => `请帮我在本机安装一个用于联网搜索的 skill。

要求：
1. 按当前环境的 skill 安装规范，把它安装成本地 skill。
2. skill 名称为：gptgrok2api-search
3. 文件名为：SKILL.md
4. 只创建或更新这个 skill 文件，不要修改其他无关文件。

SKILL.md 内容：

\`\`\`markdown
${skillZh.value}
\`\`\``)

const skillInstallPromptEn = computed(() => `Please install a local web-search skill on this machine.

Requirements:
1. Install this as a local skill according to the current environment's skill rules.
2. Skill name: gptgrok2api-search
3. File name: SKILL.md
4. Only create or update this skill file.

SKILL.md content:

\`\`\`markdown
${skillEn.value}
\`\`\``)

const currentEditableKind = computed<DebugEditableKind>(() => activeTab.value === 'psd' ? 'psd' : 'ppt')

watch(activeTab, (tab) => {
  if (tab === 'ppt') {
    editablePrompt.value = '生成一个 GPTGrok2API 项目架构说明 PPT，包含后端链路、前端控制台、账号池、日志和图片链路。'
    void refreshEditableTasks()
  } else if (tab === 'psd') {
    editablePrompt.value = '生成一个适合控制台产品展示的 PSD 版式，包含概览、账号、日志、图片和代理管理区块。'
    void refreshEditableTasks()
  }
}, { immediate: true })

onMounted(() => {
  void loadModelCatalog()
})

async function copyText(value: string) {
  try {
    await navigator.clipboard.writeText(value)
    toast.success('已复制')
  } catch {
    toast.error('复制失败')
  }
}

async function runSearch() {
  const prompt = searchPrompt.value.trim()
  if (!prompt || searchLoading.value) return
  const startedAt = Date.now()
  searchLoading.value = true
  searchError.value = ''
  searchResult.value = null
  try {
    searchResult.value = await debugApi.search(prompt)
  } catch (error: any) {
    searchError.value = error?.message || '搜索失败'
  } finally {
    searchElapsedMs.value = Date.now() - startedAt
    searchLoading.value = false
  }
}

async function sendChat() {
  const content = chatInput.value.trim()
  if (!content || chatLoading.value) return
  const nextMessages: DebugChatMessage[] = [...chatMessages.value, { role: 'user', content }]
  chatMessages.value = nextMessages
  chatInput.value = ''
  chatLoading.value = true
  chatError.value = ''
  try {
    const result = await debugApi.chat(chatModel.value, nextMessages, chatReasoningEffort.value)
    chatRaw.value = result
    chatMessages.value = [
      ...nextMessages,
      { role: 'assistant', content: String(result.choices?.[0]?.message?.content || '') },
    ]
  } catch (error: any) {
    chatError.value = error?.message || '发送失败'
  } finally {
    chatLoading.value = false
  }
}

function clearChat() {
  chatMessages.value = []
  chatRaw.value = null
  chatError.value = ''
}

function editableTaskId(task: DebugEditableFileTask) {
  return String(task.taskId || task.id || '')
}

function editableStatusLabel(status: unknown) {
  const value = String(status || 'queued')
  return { queued: '排队中', running: '生成中', success: '已完成', error: '失败' }[value] || value
}

function editableStatusTone(status: unknown): 'success' | 'danger' | 'warning' | 'muted' {
  const value = String(status || '').toLowerCase()
  if (value === 'success') return 'success'
  if (value === 'error') return 'danger'
  if (value === 'queued' || value === 'running') return 'warning'
  return 'muted'
}

async function refreshEditableTasks() {
  if (editableFetching.value) return
  editableFetching.value = true
  editableError.value = ''
  try {
    const response = await debugApi.listEditableFileTasks()
    editableTasks.value = (response.items || []).filter((task) => task.kind === currentEditableKind.value).slice(0, 20)
  } catch (error: any) {
    editableError.value = error?.message || '任务加载失败'
  } finally {
    editableFetching.value = false
  }
}

async function submitEditableTask() {
  const prompt = editablePrompt.value.trim()
  if (!prompt || editableSubmitting.value) return
  editableSubmitting.value = true
  editableError.value = ''
  try {
    const images = editableImages.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
    const task = await debugApi.createEditableFileTask(currentEditableKind.value, { prompt, base64_images: images })
    editableTasks.value = [task, ...editableTasks.value].slice(0, 20)
    toast.success('任务已提交')
  } catch (error: any) {
    editableError.value = error?.message || '提交失败'
  } finally {
    editableSubmitting.value = false
  }
}

function openUrl(url: string) {
  window.open(url, '_blank', 'noopener,noreferrer')
}
</script>

<style scoped>
.debug-center__panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 18px 20px;
}

.debug-textarea {
  width: 100%;
  min-height: 9rem;
  resize: vertical;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  background: hsl(var(--background));
  padding: 10px 12px;
  color: hsl(var(--foreground));
  font-size: 13px;
  line-height: 1.65;
  outline: none;
}

.debug-select {
  width: 100%;
  height: 2.5rem;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  background: hsl(var(--background));
  padding: 0 12px;
  color: hsl(var(--foreground));
  font-size: 13px;
  outline: none;
}

.debug-select:focus {
  border-color: hsl(var(--primary));
  box-shadow: 0 0 0 3px hsl(var(--primary) / 0.12);
}

.debug-textarea:focus {
  border-color: hsl(var(--primary));
  box-shadow: 0 0 0 3px hsl(var(--primary) / 0.12);
}

.debug-textarea--small {
  min-height: 6rem;
}

.debug-error {
  border: 1px solid hsl(var(--destructive) / 0.3);
  border-radius: 8px;
  background: hsl(var(--destructive) / 0.08);
  padding: 10px 12px;
  color: hsl(var(--destructive));
  font-size: 13px;
}

.debug-result-grid {
  display: grid;
  gap: 14px;
  grid-template-columns: minmax(0, 1fr);
}

@media (min-width: 1024px) {
  .debug-result-grid {
    grid-template-columns: minmax(0, 1fr) 20rem;
  }
}

.debug-result-card,
.debug-task-card,
.debug-chat-box {
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  background: hsl(var(--background));
  padding: 14px;
}

.debug-result-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  color: hsl(var(--muted-foreground));
  font-size: 12px;
}

.debug-answer {
  margin-top: 14px;
  white-space: pre-wrap;
  color: hsl(var(--foreground));
  font-size: 14px;
  line-height: 1.8;
}

.debug-source-link {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
  border-top: 1px solid hsl(var(--border) / 0.7);
  padding: 10px 0;
  font-size: 12px;
}

.debug-task-list {
  display: flex;
  min-height: 18rem;
  flex-direction: column;
  gap: 10px;
}

.debug-empty {
  display: flex;
  min-height: 10rem;
  align-items: center;
  justify-content: center;
  color: hsl(var(--muted-foreground));
  font-size: 13px;
}

.debug-chat-box {
  min-height: 24rem;
}

.debug-chat-message + .debug-chat-message {
  margin-top: 16px;
  border-top: 1px solid hsl(var(--border) / 0.7);
  padding-top: 14px;
}
</style>
