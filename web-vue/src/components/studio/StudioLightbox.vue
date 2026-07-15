<template>
  <GalleryLightbox
    :file="lightboxFile"
    :image-url="preview?.src || ''"
    size-label=""
    :copied="false"
    :show-actions="true"
    :show-tag-action="false"
    @close="$emit('close')"
    @copy="copyPreview"
    @download="$emit('download')"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import GalleryLightbox from '@/components/ai/GalleryLightbox.vue'
import type { GalleryFile } from '@/api/gallery'
import type { StudioPreviewImage } from './types'

const props = defineProps<{
  preview: StudioPreviewImage | null
}>()

const emit = defineEmits<{
  close: []
  copy: [value: string]
  download: []
}>()

const lightboxFile = computed<GalleryFile | null>(() => {
  const preview = props.preview
  if (!preview) return null
  return {
    filename: preview.name || 'studio-preview-image',
    path: preview.localPath || preview.src,
    url: preview.src,
    thumbnail_url: preview.src,
    size: 0,
    created_at: '',
    mtime: 0,
    date: '',
    type: 'image',
    expired: false,
    expires_in_seconds: null,
    tags: [],
    storage: 'studio',
    local: Boolean(preview.localPath),
    webdav: false,
    width: null,
    height: null,
  }
})

function copyPreview() {
  if (!props.preview?.src) return
  emit('copy', props.preview.src)
}
</script>
