import { computed, ref, watch } from 'vue'
import type { GalleryFile } from '@/api/gallery'
import type { SystemLogRow } from '@/api/logs'
import {
  buildDiagnosticDetailFields,
  buildPrimaryDetailFields,
  buildTimelineGroups,
  buildTimelineLegendItems,
  buildTimelineSegments,
  shouldAutoExpandTimeline,
  type DetailTimelineStep,
} from '@/views/logs/logDetailView'
import {
  buildLogPreviewGalleryFile,
  buildLogPreviewImages,
  type LogPreviewImage,
} from '@/views/logs/logsView'

export type DetailPreviewImage = LogPreviewImage

export function useLogDetailRuntime() {
  const selectedLog = ref<SystemLogRow | null>(null)
  const selectedDetailPreview = ref<DetailPreviewImage | null>(null)
  const timelineDetailsExpanded = ref(false)
  const brokenPreviewUrls = ref<Set<string>>(new Set())

  const selectedTimelineSegments = computed(() => buildTimelineSegments(selectedLog.value))
  const selectedTimelineLegendItems = computed(() => buildTimelineLegendItems(selectedTimelineSegments.value))
  const selectedTimelineGroups = computed(() => buildTimelineGroups(selectedLog.value))

  const selectedBottleneckStep = computed<DetailTimelineStep | null>(() => {
    const steps = selectedTimelineGroups.value.flatMap((group) => group.steps)
    return steps.reduce<DetailTimelineStep | null>((current, step) => {
      if (!current || step.valueMs > current.valueMs) return step
      return current
    }, null)
  })

  const selectedTimelineStepCount = computed(() => selectedTimelineGroups.value.reduce((total, group) => total + group.steps.length, 0))
  const selectedTimelineSegmentTotal = computed(() => selectedTimelineSegments.value.reduce((total, segment) => total + segment.valueMs, 0))
  const timelineDetailsAutoExpanded = computed(() => shouldAutoExpandTimeline(selectedLog.value, selectedBottleneckStep.value))
  const timelineDetailsVisible = computed(() => timelineDetailsExpanded.value)
  const selectedHasTimeline = computed(() => selectedTimelineSegments.value.length > 0 || selectedTimelineGroups.value.length > 0)

  const selectedPrimaryDetailFields = computed(() => buildPrimaryDetailFields(selectedLog.value))
  const selectedDiagnosticDetailFields = computed(() => buildDiagnosticDetailFields(selectedLog.value))

  const selectedDetailImages = computed(() => buildLogPreviewImages(selectedLog.value, isPreviewBroken))
  const selectedDetailPreviewFile = computed<GalleryFile | null>(() => buildLogPreviewGalleryFile(selectedDetailPreview.value))

  function isPreviewBroken(url: string): boolean {
    return brokenPreviewUrls.value.has(url)
  }

  function markPreviewBroken(event: Event, url: string) {
    const img = event.target as HTMLImageElement
    img.style.opacity = '0'
    brokenPreviewUrls.value = new Set([...brokenPreviewUrls.value, url])
  }

  function openDetail(item: SystemLogRow) {
    selectedLog.value = item
  }

  function closeDetail() {
    selectedLog.value = null
    selectedDetailPreview.value = null
  }

  function openDetailImagePreview(image: DetailPreviewImage) {
    selectedDetailPreview.value = image
  }

  function closeDetailImagePreview() {
    selectedDetailPreview.value = null
  }

  function toggleTimelineDetails() {
    timelineDetailsExpanded.value = !timelineDetailsExpanded.value
  }

  watch(
    () => selectedLog.value?.id || '',
    () => {
      timelineDetailsExpanded.value = timelineDetailsAutoExpanded.value
    },
  )

  return {
    selectedLog,
    selectedDetailPreview,
    selectedDetailPreviewFile,
    selectedDetailImages,
    selectedPrimaryDetailFields,
    selectedDiagnosticDetailFields,
    selectedTimelineSegments,
    selectedTimelineLegendItems,
    selectedTimelineGroups,
    selectedBottleneckStep,
    selectedTimelineStepCount,
    selectedTimelineSegmentTotal,
    selectedHasTimeline,
    timelineDetailsVisible,
    isPreviewBroken,
    markPreviewBroken,
    openDetail,
    closeDetail,
    openDetailImagePreview,
    closeDetailImagePreview,
    toggleTimelineDetails,
  }
}
