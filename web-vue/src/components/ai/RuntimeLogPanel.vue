<template>
  <section
    class="runtime-log-panel"
    :class="{ 'runtime-log-panel--fill': fill }"
    :style="panelStyle"
  >
    <div v-if="title || $slots.actions" class="runtime-log-panel__header">
      <span v-if="title" class="runtime-log-panel__title">{{ title }}</span>
      <div v-if="$slots.actions" class="runtime-log-panel__actions">
        <slot name="actions" />
      </div>
    </div>

    <div v-if="isEmpty" class="runtime-log-panel__empty">
      <EmptyState plain :title="emptyTitle" :description="emptyDescription" />
    </div>

    <div v-else class="runtime-log-panel__body scrollbar-slim">
      <pre v-if="hasRawText" class="runtime-log-panel__raw">{{ rawText }}</pre>
      <div v-else class="runtime-log-panel__lines">
        <div
          v-for="line in lineViews"
          :key="line.key"
          v-memo="[line.signature]"
          class="runtime-log-panel__line"
          :class="[
            line.levelClass,
            { 'runtime-log-panel__line--plain': line.plain },
          ]"
        >
          <span v-if="line.time" class="runtime-log-panel__time">{{ line.time }}</span>
          <span v-if="line.tag" class="runtime-log-panel__tag">{{ line.tag }}</span>
          <span>{{ line.text || '-' }}</span>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { EmptyState } from 'nanocat-ui'

export type RuntimeLogPanelLine = {
  key?: string
  time?: string
  tag?: string
  text: string
  level?: 'info' | 'success' | 'warning' | 'error' | string
}

const props = withDefaults(defineProps<{
  title?: string
  rawText?: string
  lines?: RuntimeLogPanelLine[]
  emptyTitle?: string
  emptyDescription?: string
  minHeight?: string
  maxHeight?: string
  fill?: boolean
}>(), {
  title: '',
  rawText: '',
  lines: () => [],
  emptyTitle: '暂无日志',
  emptyDescription: '',
  minHeight: '16rem',
  maxHeight: 'min(60vh, 42rem)',
  fill: false,
})

type RuntimeLogPanelLineView = RuntimeLogPanelLine & {
  key: string
  signature: string
  levelClass: string
  plain: boolean
}

const hasRawText = computed(() => props.rawText.trim().length > 0)
const isEmpty = computed(() => !hasRawText.value && props.lines.length === 0)
const lineViews = computed<RuntimeLogPanelLineView[]>(() => props.lines.map((line, index) => {
  const level = line.level || 'info'
  const time = line.time || ''
  const text = line.text || '-'
  return {
    ...line,
    key: line.key || `${time || 'log'}-${index}`,
    time,
    tag: line.tag || '',
    text,
    level,
    levelClass: `runtime-log-panel__line--${level}`,
    plain: !time,
    signature: runtimeLogPanelLineSignature({ ...line, time, text, level }, index),
  }
}))

const panelStyle = computed(() => ({
  '--runtime-log-min-height': props.minHeight,
  '--runtime-log-max-height': props.maxHeight,
}))

function runtimeLogPanelLineSignature(line: RuntimeLogPanelLine, index: number): string {
  return [
    line.key || index,
    line.time || '',
    line.tag || '',
    line.level || 'info',
    boundedSignatureText(line.text || '-'),
  ].map(signatureValue).join('|')
}

function signatureValue(value: unknown): string {
  return String(value ?? '').replaceAll('|', '/')
}

function boundedSignatureText(value: unknown, limit = 180): string {
  const text = signatureValue(value)
  if (text.length <= limit) return text
  return `${text.length}:${text.slice(0, limit)}:${text.slice(-24)}`
}
</script>

<style scoped>
.runtime-log-panel {
  display: flex;
  min-height: var(--runtime-log-min-height);
  max-height: var(--runtime-log-max-height);
  flex-direction: column;
  overflow: hidden;
  border: 1px solid hsl(var(--border));
  border-radius: 12px;
  background: hsl(var(--card));
}

.runtime-log-panel--fill {
  flex: 1;
  max-height: none;
}

.runtime-log-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid hsl(var(--border));
  padding: 8px 12px;
  background: hsl(var(--card));
}

.runtime-log-panel__title {
  font-size: 12px;
  font-weight: 500;
  color: hsl(var(--muted-foreground));
}

.runtime-log-panel__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.runtime-log-panel__empty {
  display: flex;
  min-height: var(--runtime-log-min-height);
  flex: 1;
  align-items: center;
  justify-content: center;
  padding: 16px;
}

.runtime-log-panel__body {
  min-height: 0;
  flex: 1;
  overflow: auto;
  background: #09090b;
  padding: 12px 16px;
  color: #fafafa;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  font-size: 12px;
  line-height: 1.65;
}

.runtime-log-panel__raw {
  min-width: max-content;
  margin: 0;
  white-space: pre;
}

.runtime-log-panel__lines {
  display: grid;
  gap: 2px;
}

.runtime-log-panel__line {
  display: grid;
  grid-template-columns: 4.75rem minmax(0, 1fr);
  gap: 10px;
  white-space: pre-wrap;
  word-break: break-word;
}

.runtime-log-panel__line:has(.runtime-log-panel__tag) {
  grid-template-columns: 4.75rem 3.75rem minmax(0, 1fr);
}

.runtime-log-panel__line--plain {
  grid-template-columns: minmax(0, 1fr);
}

.runtime-log-panel__time {
  color: rgb(161 161 170);
}

.runtime-log-panel__tag {
  overflow: hidden;
  color: rgb(212 212 216);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.runtime-log-panel__line--error {
  color: #fecdd3;
}

.runtime-log-panel__line--success {
  color: #bbf7d0;
}

.runtime-log-panel__line--warning {
  color: #fde68a;
}
</style>
