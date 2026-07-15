<template>
  <div class="space-y-6">
    <PagePanel>
      <PanelHeader title="文档中心">
        <template #copy>
          <p class="mt-1 text-xs text-muted-foreground">
            当前页面只展示 GPTGrok2API 控制台相关接口、运维边界和风险说明。
          </p>
        </template>
      </PanelHeader>

      <div class="mt-6">
        <ConsoleSegmentedTabs v-model="activeTab" :options="tabs" aria-label="文档标签" />
      </div>

      <div class="mt-6 space-y-6 text-sm text-foreground">
        <div v-if="activeTab === 'api'" class="space-y-6">
          <InfoCard title="认证方式" density="roomy">
            <p class="text-xs leading-6 text-muted-foreground">
              管理端和 OpenAI 兼容接口都使用 Bearer key。管理端登录会把 key 写入本地浏览器存储；接口调用时放在
              <code class="font-mono text-foreground">Authorization: Bearer YOUR_API_KEY</code>。
            </p>
          </InfoCard>

          <section class="grid gap-4 lg:grid-cols-2">
            <InfoCard tag="article" title="聊天模型" tone="muted">
              <div class="flex flex-wrap gap-2">
                <MetaChip v-for="model in chatModels" :key="model">
                  {{ model }}
                </MetaChip>
              </div>
            </InfoCard>

            <InfoCard tag="article" title="图片模型" tone="muted">
              <div class="flex flex-wrap gap-2">
                <MetaChip v-for="model in imageModels" :key="model">
                  {{ model }}
                </MetaChip>
              </div>
            </InfoCard>
          </section>

          <section class="space-y-2">
            <p class="text-sm font-semibold">文本对话（/v1/chat/completions）</p>
            <CodeBlock :content="chatCompletionExample" />
          </section>

          <section class="space-y-2">
            <p class="text-sm font-semibold">文生图（/v1/images/generations）</p>
            <CodeBlock :content="imageGenerationExample" />
          </section>

          <section class="space-y-2">
            <p class="text-sm font-semibold">图生图（/v1/images/edits）</p>
            <CodeBlock :content="imageEditExample" />
            <p class="mt-2 text-xs text-muted-foreground">
              也支持 image_url / image_b64，mask_url / mask_b64 同理；真实支持情况以后端版本和上游返回为准。
            </p>
          </section>
        </div>

        <div v-else-if="activeTab === 'ops'" class="grid gap-4 lg:grid-cols-2">
          <InfoCard
            v-for="section in operationSections"
            :key="section.title"
            tag="article"
            :title="section.title"
            density="roomy"
          >
            <ul class="space-y-2 text-xs leading-6 text-muted-foreground">
              <li v-for="item in section.items" :key="item">{{ item }}</li>
            </ul>
          </InfoCard>
        </div>

        <div v-else class="space-y-4">
          <InfoCard title="动作风险等级" density="roomy">
            <div class="grid gap-3">
              <div
                v-for="risk in riskRows"
                :key="risk.level"
                class="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-border bg-muted/20 px-4 py-3"
              >
                <div>
                  <p class="text-xs font-semibold text-foreground">{{ risk.level }}</p>
                  <p class="mt-1 text-xs leading-5 text-muted-foreground">{{ risk.description }}</p>
                </div>
                <MetaChip>{{ risk.policy }}</MetaChip>
              </div>
            </div>
          </InfoCard>

          <InfoCard title="当前验收状态" density="roomy">
            <div class="grid gap-3 lg:grid-cols-2">
              <div
                v-for="row in smokeRows"
                :key="row.scope"
                class="rounded-xl border border-border bg-muted/20 px-4 py-3"
              >
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <p class="text-xs font-semibold text-foreground">{{ row.scope }}</p>
                  <MetaChip>{{ row.status }}</MetaChip>
                </div>
                <p class="mt-2 text-xs leading-5 text-muted-foreground">{{ row.note }}</p>
              </div>
            </div>
          </InfoCard>

          <InfoCard title="R2 测试对象" density="roomy">
            <ul class="space-y-2 text-xs leading-6 text-muted-foreground">
              <li v-for="item in r2Requirements" :key="item">{{ item }}</li>
            </ul>
          </InfoCard>

          <InfoCard tone="muted" density="roomy" class="text-xs leading-6 text-muted-foreground">
            对话画图入口已恢复。图片生成统一走对话画图页和异步任务接口，不再让浏览器页面直接等待长时间图片请求；普通用户 key 登录后只显示对话画图页。
          </InfoCard>
        </div>
      </div>
    </PagePanel>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import CodeBlock from '@/components/ai/CodeBlock.vue'
import ConsoleSegmentedTabs from '@/components/ai/ConsoleSegmentedTabs.vue'
import InfoCard from '@/components/ai/InfoCard.vue'
import MetaChip from '@/components/ai/MetaChip.vue'
import PagePanel from '@/components/ai/PagePanel.vue'
import PanelHeader from '@/components/ai/PanelHeader.vue'
import { useSettingsStore } from '@/stores/settings'
import { useModelCatalog } from '@/composables/useModelCatalog'

const activeTab = ref('api')
const settingsStore = useSettingsStore()
const {
  chatModels,
  imageModels,
  imageEditModels,
  loadModelCatalog,
} = useModelCatalog(() => settingsStore.settings)

const tabs = [
  { value: 'api', label: '接口说明' },
  { value: 'ops', label: '运维边界' },
  { value: 'risk', label: '风险说明' },
]

const primaryChatModel = computed(() => chatModels.value[0] || 'gpt-5-mini')
const primaryImageModel = computed(() => imageModels.value[0] || 'gpt-image-2')
const primaryImageEditModel = computed(() => imageEditModels.value[0] || primaryImageModel.value)

const chatCompletionExample = computed(() => `curl -X POST "http://localhost:7860/v1/chat/completions" \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -d '{
    "model": "${primaryChatModel.value}",
    "stream": false,
    "messages": [
      { "role": "user", "content": "你好" }
    ]
  }'`)

const imageGenerationExample = computed(() => `curl -X POST "http://localhost:7860/v1/images/generations" \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -d '{
    "model": "${primaryImageModel.value}",
    "prompt": "draw a tiny cat icon, minimal flat vector",
    "n": 1,
    "size": "1024x1024",
    "response_format": "url"
  }'`)

const imageEditExample = computed(() => `curl -X POST "http://localhost:7860/v1/images/edits" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -F "model=${primaryImageEditModel.value}" \\
  -F "prompt=把这张图改成赛博风格" \\
  -F "response_format=url" \\
  -F "image=@reference.png"`)

const operationSections = [
  {
    title: '账号来源',
    items: [
      '支持 OAuth 登录已有账号、Access Token、Session JSON、CPA JSON 文件、远程 CPA 服务器和 Sub2API 服务器导入。',
      '远程 CPA 和 Sub2API 来源只在设置页配置连接，真正导入动作在账号管理页执行。',
      '批量刷新和导出账号都会先确认范围，避免误操作真实账号池。',
    ],
  },
  {
    title: '图片和存储',
    items: [
      '图库使用 /api/images 服务端分页，避免大图库一次性压到浏览器。',
      '图片保留口径使用 image_retention_days，和设置页、后端自动清理保持一致。',
      'WebDAV/R2 测试、同步、备份都属于外部副作用动作，需要确认后再执行。',
    ],
  },
  {
    title: '代理',
    items: [
      '账号代理为空时先看账号组代理，再回退默认代理；direct 表示强制直连。',
      '代理组用于维护多个出口节点；每个请求开始时选择一个节点，请求结束前不会再换。',
      '代理测试会访问外部网络；真实 smoke 需要明确测试代理和测试账号。',
      '代理组编辑保存视为确认，删除属于破坏性动作。',
    ],
  },
  {
    title: '扩展模块',
    items: [
      '对话画图入口已恢复；普通 user key 登录后只显示对话画图页，真实提交走 /api/image-tasks/*。',
      '注册账号主流程已收回到本项目内置任务；真实注册需要显式确认。',
      '真正进程运行日志已接 /api/runtime-logs 只读接口；Docker stdout/stderr 需要部署侧重定向或挂载日志文件。',
    ],
  },
]

const riskRows = [
  {
    level: 'R0 只读',
    description: '加载页面、读取列表、查看详情、读取统计。',
    policy: '可直接 smoke',
  },
  {
    level: 'R1 本地状态',
    description: '切换筛选、分页、打开弹窗、当前页勾选。',
    policy: '可直接 smoke',
  },
  {
    level: 'R2 可恢复写入',
    description: '保存设置、编辑账号、编辑标签或编辑代理组。',
    policy: '需要测试对象',
  },
  {
    level: 'R3 外部副作用',
    description: 'CPA、Sub2API、WebDAV、R2、代理测试和批量刷新。',
    policy: '需要确认配置',
  },
  {
    level: 'R4 破坏性',
    description: '删除账号、日志、图片、备份或执行图库清理。',
    policy: '只对测试数据',
  },
]

const smokeRows = [
  {
    scope: 'R0/R1 只读',
    status: '已完成',
    note: 'Dashboard、Accounts、Logs、Gallery、Proxy、Settings、Docs 已完成只读巡检，页面控制台 error 为 0。',
  },
  {
    scope: 'R2 可恢复写入',
    status: '待测试对象',
    note: '需要明确测试账号、测试图片、测试代理组和可恢复设置项后再执行。',
  },
  {
    scope: 'R3 外部副作用',
    status: '待确认配置',
    note: 'CPA、Sub2API、WebDAV、R2、代理测试和批量刷新都需要真实配置确认。',
  },
  {
    scope: 'R4 破坏性',
    status: '默认禁止',
    note: '删除账号、日志、图片、备份和图库清理只允许作用于专门创建的测试数据。',
  },
]

const r2Requirements = [
  '测试账号：用于账号编辑、代理引用、单账号刷新和恢复状态。',
  '测试图片：用于标签编辑、预览、下载和删除验证。',
  '测试代理对象：用于代理组和代理节点的新增、编辑、禁用和删除验证。',
  '测试设置项：选择低风险字段，保存前记录原值，验证后改回。',
]

onMounted(async () => {
  if (!settingsStore.settings && !settingsStore.isLoading) {
    await settingsStore.loadSettings()
  }
  await loadModelCatalog()
})
</script>
