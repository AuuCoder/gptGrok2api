<template>
  <div class="space-y-4">
    <FormSection
      v-if="mode === 'canvas'"
      title="画布入口"
      subtitle="开启后顶部导航会显示无限画布入口，并自动带上当前接口地址和密钥。"
    >
      <div class="settings-check-grid settings-check-grid--single">
        <div class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox v-model="settings.third_party_apps.infinite_canvas.enabled">
              启用无限画布入口
            </Checkbox>
          </div>
        </div>
      </div>
      <FormField label="无限画布地址">
        <Input
          v-model.trim="settings.third_party_apps.infinite_canvas.url"
          block
          placeholder="https://canvas.best"
        />
      </FormField>
    </FormSection>

    <template v-else>
      <FormSection title="接口接入" subtitle="GPT 与 Grok 共用 OpenAI 兼容接口和同一套 Bearer 鉴权。">
        <div class="grid gap-3 md:grid-cols-2">
          <SurfaceBox
            v-for="item in accessItems"
            :key="item.label"
            density="compact"
          >
            <p class="text-xs text-muted-foreground">{{ item.label }}</p>
            <p class="mt-1 break-all font-mono text-xs text-foreground">{{ item.value }}</p>
          </SurfaceBox>
        </div>
      </FormSection>

      <FormSection title="当前模型" subtitle="模型来自统一目录，会按实际 GPT/Grok 账号能力自动更新。">
        <div class="grid gap-3 md:grid-cols-3">
          <SurfaceBox v-for="item in modelItems" :key="item.label" density="compact">
            <p class="text-xs text-muted-foreground">{{ item.label }}</p>
            <p class="mt-1 break-words font-mono text-xs leading-5 text-foreground">{{ item.value }}</p>
          </SurfaceBox>
        </div>
      </FormSection>

      <FormSection title="常用接口">
        <div class="space-y-2">
          <details
            v-for="item in apiDocItems"
            :key="item.path"
            class="rounded-xl border border-border bg-card px-4 py-3"
          >
            <summary class="flex cursor-pointer list-none items-center justify-between gap-3">
              <span class="min-w-0">
                <span class="block text-sm font-medium text-foreground">{{ item.title }}</span>
                <span class="mt-1 block truncate font-mono text-xs text-muted-foreground">{{ item.method }} {{ item.path }}</span>
              </span>
              <span class="text-xs text-muted-foreground">展开</span>
            </summary>
            <div class="mt-3 space-y-2">
              <p class="text-xs leading-5 text-muted-foreground">{{ item.description }}</p>
              <pre class="overflow-auto whitespace-pre-wrap break-all rounded-xl bg-zinc-950 px-3 py-3 text-xs leading-5 text-zinc-100">{{ item.example }}</pre>
            </div>
          </details>
        </div>
      </FormSection>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { Checkbox, FormField, FormSection, Input } from 'nanocat-ui'
import SurfaceBox from '@/components/ai/SurfaceBox.vue'
import { getAuthToken } from '@/api/client'
import { useModelCatalog } from '@/composables/useModelCatalog'
import type { Settings } from '@/types/api'
import { buildApiDocItems } from '@/views/settings/settingsView'

const props = defineProps<{
  mode: 'api-docs' | 'canvas'
  settings: Settings
}>()

const serviceBaseUrl = computed(() => window.location.origin)
const openAIBaseUrl = computed(() => `${serviceBaseUrl.value.replace(/\/$/, '')}/v1`)
const currentApiKey = computed(() => getAuthToken() || '<当前密钥>')
const { chatModels, imageModels, imageEditModels, loadModelCatalog } = useModelCatalog(() => props.settings)
const primaryChatModel = computed(() => chatModels.value.find(model => model !== 'auto') || chatModels.value[0] || 'gpt-5-mini')
const primaryImageModel = computed(() => imageModels.value[0] || 'gpt-image-2')
const primaryImageEditModel = computed(() => imageEditModels.value[0] || primaryImageModel.value)
const accessItems = computed(() => [
  { label: '服务地址', value: serviceBaseUrl.value },
  { label: 'Base URL（OpenAI）', value: openAIBaseUrl.value },
  { label: 'API Key', value: currentApiKey.value },
  { label: '请求头', value: `Authorization: Bearer ${currentApiKey.value}` },
])
const modelItems = computed(() => [
  { label: '对话模型', value: chatModels.value.join(' / ') || '-' },
  { label: '生图模型', value: imageModels.value.join(' / ') || '-' },
  { label: '编辑模型', value: imageEditModels.value.join(' / ') || '-' },
])
const apiDocItems = computed(() => (
  props.mode === 'api-docs'
    ? buildApiDocItems(serviceBaseUrl.value, currentApiKey.value, {
        chat: primaryChatModel.value,
        image: primaryImageModel.value,
        imageEdit: primaryImageEditModel.value,
      })
    : []
))

onMounted(() => {
  void loadModelCatalog()
})
</script>

<style scoped>
.settings-check-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(13.5rem, 1fr));
  gap: 8px;
}

.settings-check-grid--single {
  grid-template-columns: minmax(0, 1fr);
}

.settings-check-item {
  min-height: 38px;
  border: 1px solid hsl(var(--border));
  border-radius: 14px;
  background: hsl(var(--background) / 0.72);
  transition:
    border-color 0.16s ease,
    background-color 0.16s ease;
}

.settings-check-item:hover {
  border-color: hsl(var(--foreground) / 0.18);
  background: hsl(var(--muted) / 0.24);
}

.settings-check-control {
  display: flex;
  min-height: 38px;
  align-items: center;
  gap: 8px;
  padding-right: 10px;
}

.settings-check-item :deep(label) {
  display: flex;
  width: 100%;
  flex: 1;
  min-height: 38px;
  align-items: center;
  gap: 10px;
  padding: 9px 11px;
}

.settings-check-item :deep(label > span:last-child) {
  color: hsl(var(--foreground) / 0.78);
  line-height: 1.35;
}
</style>
