import { computed, ref, watch } from 'vue'
import { promptsApi, type PromptLibraryItem, type PromptLibraryResponse, type PromptSource } from '@/api/prompts'
import { firstPromptPreviewUrl, markPromptPreviewBroken } from '@/lib/promptAssets'
import {
  ALL_PROMPT_CATEGORY,
  ALL_PROMPT_SOURCE,
  categoryOptionsFor,
  filterPromptItems,
  promptSourceLabel,
} from '@/lib/promptLibrary'

function createPromptLibraryState() {
  return {
    loading: ref(false),
    loadError: ref(''),
    prompts: ref<PromptLibraryItem[]>([]),
    sources: ref<PromptSource[]>([]),
    synced: ref(false),
  }
}

const promptLibraryState = createPromptLibraryState()
let promptLibraryRequest: Promise<PromptLibraryResponse> | null = null

function applyPromptLibrary(result: PromptLibraryResponse) {
  promptLibraryState.prompts.value = result.items
  promptLibraryState.sources.value = result.sources
  promptLibraryState.synced.value = Boolean(result.synced || result.items.length)
  promptLibraryState.loadError.value = ''
}

export async function preloadPromptLibrary(force = false) {
  const state = promptLibraryState
  if (state.loading.value && promptLibraryRequest && !force) {
    try {
      await promptLibraryRequest
      return true
    } catch {
      return false
    }
  }
  if (!force && state.prompts.value.length > 0) return true

  state.loading.value = true
  state.loadError.value = ''
  const request = promptsApi.list()
  promptLibraryRequest = request
  try {
    const result = await request
    applyPromptLibrary(result)
    return true
  } catch (error: any) {
    state.loadError.value = error?.message || '提示词加载失败，请稍后重试。'
    return false
  } finally {
    if (promptLibraryRequest === request) promptLibraryRequest = null
    state.loading.value = false
  }
}

export function usePromptLibraryRuntime() {
  const loading = promptLibraryState.loading
  const loadError = promptLibraryState.loadError
  const prompts = promptLibraryState.prompts
  const sources = promptLibraryState.sources
  const synced = promptLibraryState.synced
  const keyword = ref('')
  const sourceFilter = ref(ALL_PROMPT_SOURCE)
  const categoryFilter = ref(ALL_PROMPT_CATEGORY)
  const brokenPreviewUrls = ref<Set<string>>(new Set())

  const enabledSourceCount = computed(() => sources.value.filter((source) => source.enabled).length)
  const sourceOptions = computed(() => [
    { label: '全部来源', value: ALL_PROMPT_SOURCE },
    ...sources.value
      .filter((source) => source.enabled)
      .map((source) => ({
        label: promptSourceLabel(source),
        value: source.id,
      })),
  ])
  const scopedForCategory = computed(() => filterPromptItems(prompts.value, {
    keyword: '',
    sourceId: sourceFilter.value,
    category: ALL_PROMPT_CATEGORY,
  }))
  const categoryOptions = computed(() => categoryOptionsFor(scopedForCategory.value))
  const filteredPrompts = computed(() => filterPromptItems(prompts.value, {
    keyword: keyword.value,
    sourceId: sourceFilter.value,
    category: categoryFilter.value,
  }))

  function promptPreviewUrl(item: PromptLibraryItem) {
    return firstPromptPreviewUrl(item, brokenPreviewUrls.value)
  }

  function handlePreviewError(event: Event, item: PromptLibraryItem) {
    const primaryUrl = item.preview || item.reference_image_urls[0] || ''
    markPromptPreviewBroken(event, primaryUrl, (url) => {
      brokenPreviewUrls.value = new Set([...brokenPreviewUrls.value, url])
    })
  }

  async function loadPrompts(force = false) {
    return preloadPromptLibrary(force)
  }

  watch(sourceFilter, () => {
    categoryFilter.value = ALL_PROMPT_CATEGORY
  })

  return {
    loading,
    loadError,
    prompts,
    sources,
    synced,
    keyword,
    sourceFilter,
    categoryFilter,
    enabledSourceCount,
    sourceOptions,
    categoryOptions,
    filteredPrompts,
    promptPreviewUrl,
    handlePreviewError,
    loadPrompts,
  }
}
