<template>
  <div class="space-y-4">
    <FormSection title="图片错误提示">
      <div class="settings-check-grid settings-check-grid--single">
        <div class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox v-model="settings.image_error_friendly_enabled">启用图片错误提示友好化</Checkbox>
            <HelpTip text="关闭时保持原始错误返回；开启后按下方文案转换上游断流、轮询超时、额度耗尽等图片错误。" />
          </div>
        </div>
      </div>
    </FormSection>

    <FormSection title="自定义错误文案">
      <div class="image-error-group-list">
        <div
          v-for="group in imageErrorMessageGroups"
          :key="group.key"
          class="image-error-field-group"
        >
          <div class="image-error-group-header">
            <span class="image-error-group-title">{{ group.title }}</span>
          </div>
          <div
            class="image-error-field-grid"
            :class="{ 'image-error-field-grid--single': group.fields.length === 1 }"
          >
            <FormField
              v-for="item in group.fields"
              :key="item.key"
              :label="item.label"
            >
              <template #label-extra>
                <span class="image-error-key">{{ item.key }}</span>
                <HelpTip v-if="item.help" :text="item.help" />
              </template>
              <textarea
                v-model="settings.image_error_messages[item.key]"
                rows="3"
                class="ui-textarea-sm"
                :placeholder="item.placeholder"
                :disabled="!settings.image_error_friendly_enabled"
              ></textarea>
            </FormField>
          </div>
        </div>
      </div>
    </FormSection>
  </div>
</template>

<script setup lang="ts">
import { Checkbox, FormField, FormSection, HelpTip } from 'nanocat-ui'
import type { Settings } from '@/types/api'
import { imageErrorMessageGroups } from '@/views/settings/settingsView'

defineProps<{
  settings: Settings
}>()
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
  min-height: 36px;
  align-items: center;
  gap: 8px;
  padding: 0 10px;
}

.image-error-group-list {
  display: grid;
  gap: 16px;
}

.image-error-field-group {
  min-width: 0;
}

.image-error-field-group + .image-error-field-group {
  border-top: 1px solid hsl(var(--border) / 0.72);
  padding-top: 14px;
}

.image-error-group-header {
  display: flex;
  min-height: 20px;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.image-error-group-title {
  font-size: 12px;
  font-weight: 700;
  color: hsl(var(--muted-foreground));
  letter-spacing: 0;
}

.image-error-field-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr));
  gap: 12px;
}

.image-error-field-grid--single {
  max-width: 42rem;
}

.image-error-key {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  background: hsl(var(--muted) / 0.72);
  padding: 1px 7px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 11px;
  font-weight: 500;
  line-height: 18px;
  color: hsl(var(--muted-foreground));
}
</style>
