<template>
  <div class="settings-storage-grid">
    <FormSection title="图片存储" class="settings-storage-main">
      <div class="settings-check-grid settings-check-grid--single">
        <div class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox v-model="imageStorage.enabled">启用 WebDAV 图片存储</Checkbox>
          </div>
        </div>
      </div>

      <FormField label="存储模式">
        <div class="w-full">
          <GroupedSelectMenu
            v-model="imageStorage.mode"
            :options="imageStorageModeOptions"
            selected-indicator="none"
            aria-label="图片存储模式"
            block
          />
        </div>
      </FormField>

      <FormField label="WebDAV URL">
        <Input v-model.trim="imageStorage.webdav_url" block placeholder="https://example.com/dav" />
      </FormField>

      <div class="grid grid-cols-1 gap-2.5 md:grid-cols-2">
        <FormField label="用户名">
          <Input v-model.trim="imageStorage.webdav_username" block />
        </FormField>

        <FormField label="密码">
          <Input v-model="imageStorage.webdav_password" type="password" block />
        </FormField>
      </div>

      <FormField label="根路径">
        <Input v-model.trim="imageStorage.webdav_root_path" block placeholder="chatgpt2api/images" />
      </FormField>

      <FormField label="公开访问前缀">
        <Input v-model.trim="imageStorage.public_base_url" block placeholder="https://cdn.example.com/images" />
      </FormField>

      <div class="flex flex-wrap items-center gap-2">
        <Button size="xs" variant="outline" :disabled="imageStorageBusy === 'test'" @click="$emit('testStorage')">
          {{ imageStorageBusy === 'test' ? '测试中...' : '测试 WebDAV' }}
        </Button>
        <Button size="xs" variant="outline" :disabled="imageStorageBusy === 'sync'" @click="$emit('syncStorage')">
          {{ imageStorageBusy === 'sync' ? '同步中...' : '全量同步' }}
        </Button>
      </div>

      <div v-if="imageStorageTestResult" class="rounded-xl border border-border bg-background px-3 py-2 text-xs">
        <p :class="imageStorageTestResult.ok ? 'text-emerald-600' : 'text-slate-600'">
          {{ imageStorageTestResult.ok ? 'WebDAV 可用' : 'WebDAV 不可用' }}
          <span v-if="imageStorageTestResult.status"> · HTTP {{ imageStorageTestResult.status }}</span>
        </p>
        <p v-if="imageStorageTestResult.error" class="mt-1 break-all text-slate-600">{{ imageStorageTestResult.error }}</p>
      </div>
    </FormSection>

    <FormSection title="AI 审核" class="settings-storage-side">
      <div class="settings-check-grid settings-check-grid--single">
        <div class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox v-model="settings.ai_review.enabled">启用 AI 审核</Checkbox>
          </div>
        </div>
      </div>

      <FormField label="Base URL">
        <Input v-model.trim="settings.ai_review.base_url" block placeholder="https://api.openai.com" />
      </FormField>

      <FormField label="API Key">
        <Input v-model="settings.ai_review.api_key" type="password" block placeholder="sk-..." />
      </FormField>

      <FormField label="Model">
        <Input v-model.trim="settings.ai_review.model" block placeholder="填写审核服务支持的模型 ID" />
      </FormField>

      <FormField label="审核提示词">
        <textarea
          v-model="settings.ai_review.prompt"
          rows="5"
          class="ui-textarea-sm"
          placeholder="判断用户请求是否允许。只回答 ALLOW 或 REJECT。"
        ></textarea>
      </FormField>
    </FormSection>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Button, Checkbox, FormField, FormSection, Input } from 'nanocat-ui'
import type { ImageStorageTestResult } from '@/api/settings'
import GroupedSelectMenu from '@/components/ui/GroupedSelectMenu.vue'
import type { Settings } from '@/types/api'
import { imageStorageModeOptions } from '@/views/settings/settingsView'

const props = defineProps<{
  settings: Settings
  imageStorageBusy: string
  imageStorageTestResult: ImageStorageTestResult | null
}>()

defineEmits<{
  testStorage: []
  syncStorage: []
}>()

const imageStorage = computed(() => props.settings.image_storage as NonNullable<Settings['image_storage']>)
</script>

<style scoped>
.settings-storage-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

@media (min-width: 1280px) {
  .settings-storage-grid {
    grid-template-columns: minmax(0, 2fr) minmax(20rem, 1fr);
  }
}

.settings-storage-main,
.settings-storage-side {
  min-width: 0;
}

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
  min-height: 36px;
  align-items: center;
  gap: 8px;
  padding: 0 10px;
}
</style>
