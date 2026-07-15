<template>
  <PagePanel flush>
    <TableShell>
      <table class="w-full min-w-[1240px] table-fixed text-left">
        <colgroup>
          <col class="w-12" />
          <col class="w-36" />
          <col class="w-24" />
          <col class="w-40" />
          <col class="w-44" />
          <col class="w-28" />
          <col class="w-24" />
          <col class="w-28" />
          <col />
          <col class="w-36" />
        </colgroup>
        <thead class="bg-muted/40 text-xs text-muted-foreground">
          <tr>
            <th class="py-3 pl-4 pr-2">
              <Checkbox
                :model-value="allVisibleLogsSelected"
                :disabled="visibleLogs.length === 0"
                @update:model-value="emit('toggle-select-all-visible', $event)"
              >
                <span class="sr-only">全选当前页日志</span>
              </Checkbox>
            </th>
            <th class="py-3 pr-5">时间</th>
            <th class="py-3 pr-5">类型</th>
            <th class="py-3 pr-5">令牌名称</th>
            <th class="py-3 pr-5">调用账号</th>
            <th class="py-3 pr-5">调用耗时</th>
            <th class="py-3 pr-5">状态</th>
            <th class="py-3 pr-5">图片</th>
            <th class="py-3 pr-5">简述</th>
            <th class="py-3 pr-4 text-right">操作</th>
          </tr>
        </thead>
        <tbody class="text-sm text-foreground">
          <tr v-if="!isFetching && logs.length === 0">
            <td colspan="10" class="py-8">
              <EmptyState
                plain
                :title="logsLoadError ? '日志加载失败' : '暂无日志'"
                :description="logsLoadError || '换个筛选条件或刷新后再看。'"
              />
            </td>
          </tr>
          <LogsSystemRow
            v-for="item in visibleLogs"
            :key="item.id"
            :item="item"
            :signature="rowSignature(item)"
            :selected="isLogSelected(item.id)"
            :first-image-broken="isPreviewBroken(item.imageUrls[0] || '')"
            @toggle-selection="handleToggleLogSelection"
            @open-detail="emit('open-detail', $event)"
            @request-delete-log="emit('request-delete-log', $event)"
            @image-error="emit('image-error', $event)"
          />
        </tbody>
      </table>

      <template #footer>
        <ListPagination
          :page="page"
          :page-size="pageSize"
          :total-count="totalCount"
          :page-size-options="systemLogPageSizeOptions"
          unit="条日志"
          :disabled="isFetching"
          @update:page="emit('update:page', $event)"
          @update:page-size="emit('update:pageSize', $event)"
        />
      </template>
    </TableShell>
  </PagePanel>
</template>

<script setup lang="ts">
import { Checkbox, EmptyState } from 'nanocat-ui'

import ListPagination from '@/components/ai/ListPagination.vue'
import PagePanel from '@/components/ai/PagePanel.vue'
import TableShell from '@/components/ai/TableShell.vue'
import type { SystemLogRow } from '@/api/logs'
import LogsSystemRow from '@/views/logs/LogsSystemRow.vue'
import {
  systemLogPageSizeOptions,
  systemLogRowSignature,
} from '@/views/logs/logsView'

const props = defineProps<{
  visibleLogs: SystemLogRow[]
  logs: SystemLogRow[]
  isFetching: boolean
  logsLoadError: string
  allVisibleLogsSelected: boolean
  page: number
  pageSize: number
  totalCount: number
  isLogSelected: (id: string) => boolean
  isPreviewBroken: (url: string) => boolean
}>()

const emit = defineEmits<{
  (e: 'update:page', value: number): void
  (e: 'update:pageSize', value: number): void
  (e: 'toggle-select-all-visible', checked: boolean): void
  (e: 'toggle-log-selection', id: string, checked: boolean): void
  (e: 'open-detail', item: SystemLogRow): void
  (e: 'request-delete-log', item: SystemLogRow): void
  (e: 'image-error', url: string): void
}>()

function rowSignature(item: SystemLogRow) {
  return systemLogRowSignature(item, {
    selected: props.isLogSelected(item.id),
    firstImageBroken: props.isPreviewBroken(item.imageUrls[0] || ''),
  })
}

function handleToggleLogSelection(id: string, checked: boolean) {
  emit('toggle-log-selection', id, checked)
}
</script>
