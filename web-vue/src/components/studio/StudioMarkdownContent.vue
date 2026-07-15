<template>
  <div class="markdown-body chat-markdown" dir="auto" v-html="html" @click="handleMarkdownClick"></div>
</template>

<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import {
  needsStudioCodeFormatter,
  renderStudioMarkdown,
  type StudioCodeFormatter,
} from '@/lib/studioMarkdownRenderer'
import { loadStudioCodeFormatter } from '@/lib/studioCodeFormatter'

const props = defineProps<{
  content: string
  status?: string
}>()

const emit = defineEmits<{
  'citation-click': [href: string]
}>()

const codeFormatter = shallowRef<StudioCodeFormatter | null>(null)
const isLoadingCodeFormatter = shallowRef(false)

const html = computed(() => {
  const isStreaming = isStreamingStatus(props.status)
  return renderStudioMarkdown(props.content || '', {
    cache: !isStreaming,
    highlight: !isStreaming,
    codeFormatter: isStreaming ? null : codeFormatter.value,
  })
})

watch(
  () => [props.content, props.status] as const,
  async ([content, status]) => {
    if (
      codeFormatter.value
      || isLoadingCodeFormatter.value
      || isStreamingStatus(status)
      || !needsStudioCodeFormatter(content)
    ) return

    isLoadingCodeFormatter.value = true
    try {
      codeFormatter.value = await loadStudioCodeFormatter()
    } catch {
      codeFormatter.value = null
    } finally {
      isLoadingCodeFormatter.value = false
    }
  },
  { immediate: true },
)

function isStreamingStatus(status?: string) {
  return status === 'streaming' || status === 'sending'
}

async function handleMarkdownClick(event: MouseEvent) {
  const target = event.target as HTMLElement | null
  const citationLink = target?.closest<HTMLAnchorElement>('a[href^="studio-citation:"]')
  if (citationLink) {
    event.preventDefault()
    emit('citation-click', citationLink.getAttribute('href') || '')
    return
  }
  const button = target?.closest<HTMLButtonElement>('.studio-code-copy')
  if (!button) return
  event.preventDefault()
  event.stopPropagation()
  const block = button.closest('.studio-code-shell')
  const code = block?.querySelector('code')?.textContent || ''
  if (!code) return
  try {
    await writeClipboardText(code)
    button.textContent = '已复制'
    window.setTimeout(() => {
      button.textContent = '复制'
    }, 1200)
  } catch {
    button.textContent = '复制失败'
    window.setTimeout(() => {
      button.textContent = '复制'
    }, 1200)
  }
}

async function writeClipboardText(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  const scrollX = window.scrollX
  const scrollY = window.scrollY
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', 'readonly')
  textarea.style.position = 'fixed'
  textarea.style.top = '0'
  textarea.style.left = '0'
  textarea.style.width = '1px'
  textarea.style.height = '1px'
  textarea.style.opacity = '0'
  textarea.style.pointerEvents = 'none'
  document.body.appendChild(textarea)
  let ok = false
  try {
    textarea.focus({ preventScroll: true })
    textarea.select()
    ok = document.execCommand('copy')
  } finally {
    document.body.removeChild(textarea)
    window.scrollTo(scrollX, scrollY)
  }
  if (!ok) throw new Error('copy failed')
}
</script>
