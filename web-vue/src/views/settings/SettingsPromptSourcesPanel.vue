<template>
  <div class="settings-prompt-sources">
    <div class="settings-prompt-sources-head">
      <div>
        <p class="ui-section-title">提示词源</p>
        <p class="mt-1 text-xs text-muted-foreground">
          Studio 从本地快照读取提示词；这里仅负责启用词源并手动同步到本地。
        </p>
      </div>
      <div class="settings-prompt-sources-actions">
        <Button size="sm" variant="primary" :disabled="refreshing === 'all' || loading" @click="refreshAll">
          {{ refreshing === 'all' ? '更新中...' : '更新启用词源' }}
        </Button>
      </div>
    </div>

    <PageLoadingState
      v-if="loading && sources.length === 0"
      compact
      title="正在读取提示词源"
      description="只读取本地词源配置和同步状态。"
    />

    <div v-else class="settings-prompt-source-list">
      <article
        v-for="source in sources"
        :key="source.id"
        class="settings-prompt-source"
        :class="{ 'is-disabled': !source.enabled }"
      >
        <div class="settings-prompt-source-main">
          <div class="settings-prompt-source-title">
            <span class="settings-prompt-source-name">{{ source.name || source.id }}</span>
            <MetaChip
              v-if="source.last_error"
              size="xs"
              :tone="source.prompt_count > 0 ? 'warning' : 'danger'"
              variant="outline"
              :title="source.last_error"
            >
              {{ source.prompt_count > 0 ? '缓存可用' : '同步失败' }}
            </MetaChip>
            <MetaChip v-if="source.built_in" size="xs" tone="muted" variant="outline">
              内置
            </MetaChip>
          </div>
          <div class="settings-prompt-source-line">
            <a :href="source.homepage || source.url" target="_blank" rel="noreferrer" class="settings-prompt-source-url">
              {{ source.url }}
            </a>
          </div>
        </div>

        <div class="settings-prompt-source-side">
          <div class="settings-prompt-source-meta">
            <MetaChip size="xs" tone="muted">{{ source.prompt_count }} 条</MetaChip>
            <span class="settings-prompt-source-meta-text" :title="source.last_error || ''">
              {{ source.last_error ? '缓存' : '同步' }} {{ promptSyncTime(source.last_sync_at) }}
            </span>
            <span v-if="source.last_fetch_ms" class="settings-prompt-source-meta-text" :title="source.last_error || ''">
              {{ source.last_error ? '失败 ' : '' }}{{ source.last_fetch_ms }}ms
            </span>
          </div>

          <div class="settings-prompt-source-actions">
            <Checkbox
              :model-value="source.enabled"
              :disabled="busySourceId === source.id || refreshing === source.id"
              @update:model-value="toggleSource(source, Boolean($event))"
            >
              启用
            </Checkbox>
            <Button
              size="xs"
              variant="outline"
              :disabled="refreshing === source.id || busySourceId === source.id"
              @click="refreshOne(source)"
            >
              {{ refreshing === source.id ? '更新中' : '更新' }}
            </Button>
          </div>
        </div>
      </article>

      <StateBlock v-if="!loading && sources.length === 0" compact dashed>
        暂无提示词源。
      </StateBlock>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Button, Checkbox } from 'nanocat-ui'
import { promptsApi, type PromptSource } from '@/api/prompts'
import MetaChip from '@/components/ai/MetaChip.vue'
import PageLoadingState from '@/components/ai/PageLoadingState.vue'
import StateBlock from '@/components/ai/StateBlock.vue'
import { preloadPromptLibrary } from '@/composables/usePromptLibraryRuntime'
import { useToast } from '@/composables/useToast'
import { promptSyncTime } from '@/lib/promptLibrary'

const toast = useToast()
const sources = ref<PromptSource[]>([])
const loading = ref(false)
const refreshing = ref('')
const busySourceId = ref('')

async function reloadStudioPromptLibrary() {
  await preloadPromptLibrary(true).catch(() => {})
}

async function loadSources() {
  if (loading.value) return
  loading.value = true
  try {
    const result = await promptsApi.listSources()
    sources.value = result.sources
  } catch (error: any) {
    toast.error(error?.message || '提示词源读取失败')
  } finally {
    loading.value = false
  }
}

async function toggleSource(source: PromptSource, enabled: boolean) {
  if (busySourceId.value) return
  busySourceId.value = source.id
  try {
    const result = await promptsApi.updateSource(source.id, { enabled })
    sources.value = result.sources
    await reloadStudioPromptLibrary()
    toast.success(enabled ? '提示词源已启用' : '提示词源已停用')
  } catch (error: any) {
    toast.error(error?.message || '提示词源更新失败')
  } finally {
    busySourceId.value = ''
  }
}

async function refreshOne(source: PromptSource) {
  if (refreshing.value) return
  refreshing.value = source.id
  try {
    const result = await promptsApi.refreshSource(source.id)
    sources.value = result.sources
    await reloadStudioPromptLibrary()
    const current = result.sources.find((item) => item.id === source.id)
    if (current?.last_error) {
      toast.error(`${current.name || source.name || '提示词源'} 同步失败，已保留本地缓存`)
    } else {
      toast.success('提示词源已更新到本地')
    }
  } catch (error: any) {
    toast.error(error?.message || '提示词源更新失败')
    await loadSources()
  } finally {
    refreshing.value = ''
  }
}

async function refreshAll() {
  if (refreshing.value) return
  refreshing.value = 'all'
  try {
    const result = await promptsApi.refreshSources()
    sources.value = result.sources
    await reloadStudioPromptLibrary()
    const failedSources = result.sources.filter((source) => source.enabled && source.last_error)
    if (failedSources.length > 0) {
      toast.error(`${failedSources.length} 个词源同步失败，已保留本地缓存`)
    } else {
      toast.success('启用词源已更新到本地')
    }
  } catch (error: any) {
    toast.error(error?.message || '提示词源更新失败')
    await loadSources()
  } finally {
    refreshing.value = ''
  }
}

onMounted(() => {
  void loadSources()
})
</script>

<style scoped>
.settings-prompt-sources {
  display: grid;
  gap: 0.8rem;
}

.settings-prompt-sources-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.75rem;
}

.settings-prompt-sources-actions {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 0.45rem;
}

.settings-prompt-source-list {
  display: grid;
  gap: 0.5rem;
}

.settings-prompt-source {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 1rem;
  align-items: center;
  border: 1px solid hsl(var(--border));
  border-radius: 8px;
  background: hsl(var(--card));
  padding: 0.72rem 0.8rem;
  transition:
    background-color 0.16s ease,
    border-color 0.16s ease;
}

.settings-prompt-source:hover {
  border-color: hsl(var(--border) / 0.86);
  background: hsl(var(--muted) / 0.18);
}

.settings-prompt-source.is-disabled {
  background: hsl(var(--muted) / 0.18);
}

.settings-prompt-source-main {
  min-width: 0;
}

.settings-prompt-source-title {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 0.36rem;
  flex-wrap: wrap;
}

.settings-prompt-source-name {
  min-width: 0;
  overflow: hidden;
  color: hsl(var(--foreground));
  font-size: 0.86rem;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.settings-prompt-source-line {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 0.45rem;
  margin-top: 0.16rem;
}

.settings-prompt-source-url {
  display: block;
  min-width: 0;
  flex: 1;
  overflow: hidden;
  color: hsl(var(--muted-foreground));
  font-size: 0.74rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.settings-prompt-source-url:hover {
  color: hsl(var(--foreground));
}

.settings-prompt-source-meta {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 0.3rem 0.45rem;
  color: hsl(var(--muted-foreground));
  font-size: 0.74rem;
  line-height: 1;
  white-space: nowrap;
}

.settings-prompt-source-meta-text {
  display: inline-flex;
  align-items: center;
  min-height: 1.18rem;
}

.settings-prompt-source-side {
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: flex-end;
  gap: 0.8rem;
}

.settings-prompt-source-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: flex-end;
  gap: 0.55rem;
}

@media (max-width: 900px) {
  .settings-prompt-sources-head,
  .settings-prompt-source {
    grid-template-columns: minmax(0, 1fr);
  }

  .settings-prompt-sources-head {
    display: grid;
  }

  .settings-prompt-sources-actions {
    justify-content: flex-start;
  }

  .settings-prompt-source-meta {
    justify-content: flex-start;
    min-width: 0;
  }

  .settings-prompt-source-side {
    min-width: 0;
    align-items: center;
    justify-content: flex-start;
    flex-wrap: wrap;
  }

  .settings-prompt-source-actions {
    justify-content: flex-start;
  }
}
</style>
