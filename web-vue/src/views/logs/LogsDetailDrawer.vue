<template>
  <ModalShell
    :open="Boolean(log)"
    max-width="54rem"
    :z-index="130"
    align="start"
    placement="end"
    panel-class="flex h-[calc(100vh-32px)] max-h-[calc(100vh-32px)] flex-col"
    close-on-backdrop
    @close="emit('close')"
  >
    <template v-if="log">
      <ModalHeader title="日志详情" @close="emit('close')" />

      <div class="scrollbar-slim flex-1 space-y-5 overflow-y-auto px-5 py-4">
        <section class="log-detail-summary">
          <div class="log-detail-summary__main">
            <div class="log-detail-summary__copy">
              <div class="log-detail-summary__title-row">
                <StateBadge :tone="statusTone(log)" shape="rounded" :bordered="false">
                  {{ statusLabel(log) }}
                </StateBadge>
              </div>
              <p class="log-detail-summary__title">
                {{ summaryText(log) || '调用日志' }}
              </p>
            </div>
            <div class="log-detail-summary__duration">
              <span>总耗时</span>
              <strong>{{ formatTimelineMs(log.durationMs) }}</strong>
            </div>
          </div>
        </section>

        <div class="detail-field-stack">
          <section class="detail-field-section">
            <div class="detail-field-section__header">
              <span>请求身份</span>
            </div>
            <div class="detail-field-grid">
              <DetailFieldCard
                v-for="field in primaryFields"
                :key="field.label"
                :class="{ 'detail-field-grid__item--wide': field.wide }"
                :label="field.label"
                :value="field.value"
                :copyable="field.copyable"
                variant="row"
                @copy="emit('copy', $event)"
              />
            </div>
          </section>

          <section v-if="diagnosticFields.length" class="detail-field-section">
            <div class="detail-field-section__header detail-field-section__header--muted">
              <span>诊断字段</span>
            </div>
            <div class="detail-field-grid detail-field-grid--diagnostic">
              <DetailFieldCard
                v-for="field in diagnosticFields"
                :key="field.label"
                :label="field.label"
                :value="field.value"
                :copyable="field.copyable"
                variant="row"
                @copy="emit('copy', $event)"
              />
            </div>
          </section>
        </div>

        <LogsDetailTimeline
          v-if="hasTimeline"
          :segments="timelineSegments"
          :legend-items="timelineLegendItems"
          :groups="timelineGroups"
          :bottleneck-step="bottleneckStep"
          :step-count="timelineStepCount"
          :segment-total-ms="timelineSegmentTotal"
          :details-visible="timelineDetailsVisible"
          @toggle-details="emit('toggle-timeline-details')"
        />

        <DetailTextBlock
          :title="log.requestTextTruncated ? '请求文本（已截断）' : '请求文本'"
          :content="log.requestTextFull || log.requestText"
          @copy="emit('copy', $event)"
        />
        <DetailTextBlock
          title="错误"
          :content="log.error"
          tone="danger"
          @copy="emit('copy', $event)"
        />
        <DetailTextBlock
          title="原始上游错误"
          :content="log.rawUpstreamError"
          tone="danger"
          @copy="emit('copy', $event)"
        />
        <DetailTextBlock
          title="上游文本回复"
          :content="log.rawUpstreamMessage || log.upstreamPreview"
          tone="warning"
          @copy="emit('copy', $event)"
        />
        <DetailImagePreview
          :images="images"
          @image-error="(event, url) => emit('image-error', event, url)"
          @preview-click="emit('preview-image', $event)"
        />
        <DetailTextBlock
          title="结果 URL"
          :content="log.urls.join('\n')"
          @copy="emit('copy', $event)"
        />

        <DetailTextBlock
          title="原始 detail JSON"
          :content="log.rawJson"
          tone="muted"
          max-height="24rem"
          @copy="emit('copy', $event)"
        />
      </div>
    </template>
  </ModalShell>
</template>

<script setup lang="ts">
import DetailFieldCard from '@/components/ai/DetailFieldCard.vue'
import DetailImagePreview from '@/components/ai/DetailImagePreview.vue'
import DetailTextBlock from '@/components/ai/DetailTextBlock.vue'
import ModalHeader from '@/components/ai/ModalHeader.vue'
import ModalShell from '@/components/ai/ModalShell.vue'
import StateBadge from '@/components/ai/StateBadge.vue'
import type { SystemLogRow } from '@/api/logs'
import {
  formatTimelineMs,
  type DetailField,
  type DetailTimelineGroup,
  type DetailTimelineLegendItem,
  type DetailTimelineSegment,
  type DetailTimelineStep,
} from '@/views/logs/logDetailView'
import type { DetailPreviewImage } from '@/views/logs/logDetailRuntime'
import { statusLabel, statusTone, summaryText } from '@/views/logs/logsView'
import LogsDetailTimeline from '@/views/logs/LogsDetailTimeline.vue'

defineProps<{
  log: SystemLogRow | null
  primaryFields: DetailField[]
  diagnosticFields: DetailField[]
  timelineSegments: DetailTimelineSegment[]
  timelineLegendItems: DetailTimelineLegendItem[]
  timelineGroups: DetailTimelineGroup[]
  bottleneckStep: DetailTimelineStep | null
  timelineStepCount: number
  timelineSegmentTotal: number
  hasTimeline: boolean
  timelineDetailsVisible: boolean
  images: DetailPreviewImage[]
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'copy', value: string): void
  (e: 'image-error', event: Event, url: string): void
  (e: 'preview-image', image: DetailPreviewImage): void
  (e: 'toggle-timeline-details'): void
}>()
</script>

<style scoped>
.log-detail-summary {
  display: flex;
  flex-direction: column;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  background:
    linear-gradient(180deg, hsl(var(--muted) / 0.34), transparent 72%),
    hsl(var(--card));
  padding: 12px 14px;
}

.log-detail-summary__main {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.log-detail-summary__copy {
  min-width: 0;
}

.log-detail-summary__title-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.log-detail-summary__title {
  margin-top: 8px;
  overflow-wrap: anywhere;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.55;
  color: hsl(var(--foreground));
}

.log-detail-summary__duration {
  display: flex;
  min-width: 5.5rem;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  text-align: right;
}

.log-detail-summary__duration span {
  font-size: 11px;
  color: hsl(var(--muted-foreground));
}

.log-detail-summary__duration strong {
  font-size: 20px;
  font-weight: 650;
  color: hsl(var(--foreground));
}

.detail-field-stack {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.detail-field-section {
  border: 1px solid hsl(var(--border));
  border-radius: 12px;
  background: hsl(var(--card));
  padding: 12px;
}

.detail-field-section__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  color: hsl(var(--foreground));
  font-size: 12px;
  font-weight: 600;
}

.detail-field-section__header--muted {
  color: hsl(var(--muted-foreground));
}

.detail-field-grid {
  display: grid;
  gap: 8px;
}

.detail-field-grid--diagnostic {
  gap: 6px;
}

.detail-field-grid__item--wide {
  grid-column: 1 / -1;
}

.detail-field-grid > .detail-field-grid__item--wide.detail-field-card--row {
  grid-template-columns: 4.8rem minmax(0, 1fr);
}

@media (min-width: 640px) {
  .detail-field-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .log-detail-summary__main {
    flex-direction: column;
  }

  .log-detail-summary__duration {
    align-items: flex-start;
    text-align: left;
  }
}
</style>
