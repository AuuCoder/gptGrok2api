import { computed } from 'vue'
import type { ActionMenuItem } from 'nanocat-ui'

import { actionMenuGroups } from '@/components/ai/menuItems'
import type { AccountBulkAction } from './accountBulkActionsRuntime'
import type { AccountImportMode } from './accountImportRuntime'

type ReadableRef<T> = {
  readonly value: T
}

type WritableRef<T> = {
  value: T
}

type AccountGroupBindOption = {
  label: string
  value: string
}

type AccountExportScope = 'selected' | 'all'

export type AccountActionMenuItem = ActionMenuItem & {
  children?: AccountActionMenuItem[]
}

type AccountActionMenuRuntimeOptions = {
  selectedCount: ReadableRef<number>
  accountAllTotal: ReadableRef<number>
  accountGroupsLoading: ReadableRef<boolean>
  bindAccountGroupOptions: ReadableRef<readonly AccountGroupBindOption[]>
  selectedBindGroupId: WritableRef<string>
  openCreateModal: () => void
  openImportModal: (mode: AccountImportMode) => void
  exportAccounts: (scope: AccountExportScope) => Promise<void>
  refreshAllAccounts: () => Promise<void>
  extractSelectedCheckout: () => Promise<void>
  runBulkAction: (action: AccountBulkAction) => Promise<void>
  bindSelectedAccountsToGroup: () => Promise<void>
}

const BIND_ACCOUNT_GROUP_ACTION_PREFIX = 'bind_group:'

const accountImportActions = new Set<AccountImportMode>([
  'oauth_login',
  'access_token',
  'session_json',
  'cpa_json',
  'remote_cpa',
  'sub2api',
])

const accountBulkActions = new Set<AccountBulkAction>([
  'refresh',
  'reset',
  'enable',
  'disable',
  'delete',
])

function isAccountImportAction(value: string): value is AccountImportMode {
  return accountImportActions.has(value as AccountImportMode)
}

function isAccountBulkAction(value: string): value is AccountBulkAction {
  return accountBulkActions.has(value as AccountBulkAction)
}

export function useAccountActionMenuRuntime(options: AccountActionMenuRuntimeOptions) {
  const bindAccountGroupBatchItems = computed<AccountActionMenuItem[]>(() => {
    const disabled = options.selectedCount.value === 0 || options.accountGroupsLoading.value
    const normalOptions = options.bindAccountGroupOptions.value.filter((option) => (
      option.value && option.value !== '__ungrouped__'
    ))
    const ungroupedOptions = options.bindAccountGroupOptions.value.filter((option) => (
      option.value === '__ungrouped__'
    ))
    const children = actionMenuGroups<AccountActionMenuItem>(
      normalOptions.map((option) => ({
        key: `${BIND_ACCOUNT_GROUP_ACTION_PREFIX}${option.value}`,
        label: `绑定到 ${option.label}`,
        disabled,
      })),
      ungroupedOptions.map((option) => ({
        key: `${BIND_ACCOUNT_GROUP_ACTION_PREFIX}${option.value}`,
        label: option.label,
        disabled,
      })),
    )

    return [{
      key: 'bind_group_menu',
      label: '绑定分组',
      disabled: disabled || children.length === 0,
      children,
    }]
  })

  const accountEntryItems = computed<ActionMenuItem[]>(() => actionMenuGroups(
    [
      { key: 'create', label: '手动添加账号' },
    ],
    [
      { key: 'oauth_login', label: 'OAuth 登录已有账号' },
      { key: 'access_token', label: '导入 Access Token' },
      { key: 'session_json', label: '导入 Session JSON' },
      { key: 'cpa_json', label: '导入 CPA JSON 文件' },
      { key: 'remote_cpa', label: '从远程 CPA 服务器导入' },
      { key: 'sub2api', label: '从 Sub2API 服务器导入' },
    ],
  ))

  const exportMenuItems = computed<ActionMenuItem[]>(() => actionMenuGroups(
    [
      {
        key: 'selected',
        label: `导出选中${options.selectedCount.value ? ` (${options.selectedCount.value})` : ''}`,
        disabled: options.selectedCount.value === 0,
      },
    ],
    [
      {
        key: 'all',
        label: '导出全部',
        disabled: options.accountAllTotal.value === 0,
      },
    ],
  ))

  const batchMenuItems = computed<AccountActionMenuItem[]>(() => actionMenuGroups<AccountActionMenuItem>(
    [
      { key: 'extract_checkout', label: '批量持续提链' },
      { key: 'refresh', label: '批量刷新账号信息和额度' },
      { key: 'reset', label: '批量重置' },
    ],
    bindAccountGroupBatchItems.value,
    [
      { key: 'enable', label: '批量启用' },
      { key: 'disable', label: '批量禁用' },
      { key: 'delete', label: '批量删除', danger: true },
    ],
  ))

  const toolbarBatchMenuItems = computed<AccountActionMenuItem[]>(() => {
    const noSelection = options.selectedCount.value === 0
    return actionMenuGroups<AccountActionMenuItem>(
      [
        {
          key: 'refresh_all',
          label: '刷新全部账号信息和额度',
          disabled: options.accountAllTotal.value === 0,
        },
      ],
      [
        {
          key: 'extract_checkout',
          label: `持续提链选中账号${options.selectedCount.value ? ` (${options.selectedCount.value})` : ''}`,
          disabled: noSelection,
        },
        {
          key: 'refresh',
          label: `刷新选中${options.selectedCount.value ? ` (${options.selectedCount.value})` : ''}`,
          disabled: noSelection,
        },
        { key: 'reset', label: '重置选中状态', disabled: noSelection },
      ],
      bindAccountGroupBatchItems.value,
      [
        { key: 'enable', label: '启用选中', disabled: noSelection },
        { key: 'disable', label: '禁用选中', disabled: noSelection },
        { key: 'delete', label: '删除选中', disabled: noSelection, danger: true },
      ],
    )
  })

  async function handleBatchAction(action: string) {
    if (action === 'extract_checkout') {
      await options.extractSelectedCheckout()
      return
    }
    if (action.startsWith(BIND_ACCOUNT_GROUP_ACTION_PREFIX)) {
      options.selectedBindGroupId.value = action.slice(BIND_ACCOUNT_GROUP_ACTION_PREFIX.length)
      await options.bindSelectedAccountsToGroup()
      return
    }
    if (isAccountBulkAction(action)) {
      await options.runBulkAction(action)
    }
  }

  async function handleToolbarBatchAction(action: string) {
    if (action === 'refresh_all') {
      await options.refreshAllAccounts()
      return
    }
    await handleBatchAction(action)
  }

  function handleAccountEntryAction(key: string) {
    if (key === 'create') {
      options.openCreateModal()
      return
    }
    if (isAccountImportAction(key)) {
      options.openImportModal(key)
    }
  }

  async function handleExportAction(key: string) {
    if (key === 'selected' || key === 'all') {
      await options.exportAccounts(key)
    }
  }

  return {
    accountEntryItems,
    exportMenuItems,
    batchMenuItems,
    toolbarBatchMenuItems,
    handleBatchAction,
    handleToolbarBatchAction,
    handleAccountEntryAction,
    handleExportAction,
  }
}
