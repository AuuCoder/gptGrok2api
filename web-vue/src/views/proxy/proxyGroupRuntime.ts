import { computed, reactive, ref } from 'vue'

import { proxyApi, type ProxyGroup, type ProxyNode, type ProxyTestResult } from '@/api/proxy'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'
import {
  proxyGroupActionItems as buildProxyGroupActionItems,
  proxyGroupReference,
  proxyNodeTestClass,
  proxyNodeTestSummary,
} from '@/views/proxy/proxyView'

export type ProxyGroupForm = {
  id: string
  name: string
  enabled: boolean
  notes: string
  nodes: ProxyNode[]
}

export const FORM_TEST_KEY = '__form__'

const DEFAULT_PROXY_NODE_IMAGE_CONCURRENCY = 30

function createGeneratedId(prefix: string) {
  let suffix = ''
  try {
    suffix = globalThis.crypto?.randomUUID?.().replace(/-/g, '').slice(0, 10) || ''
  } catch {
    suffix = ''
  }
  if (!suffix) {
    suffix = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`.slice(0, 10)
  }
  return `${prefix}-${suffix}`
}

function normalizeReferenceId(value: string) {
  return value
    .trim()
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^[-._]+|[-._]+$/g, '')
    .slice(0, 64)
}

export function normalizeGroupId(value: string) {
  return normalizeReferenceId(value)
}

export function normalizeImageConcurrencyLimit(value: unknown) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return 0
  return Math.max(0, Math.min(10000, Math.floor(parsed)))
}

function createDefaultNode(index = 0): ProxyNode {
  return {
    id: createGeneratedId('node'),
    name: `出口 ${index + 1}`,
    url: '',
    enabled: true,
    image_concurrency_limit: DEFAULT_PROXY_NODE_IMAGE_CONCURRENCY,
    notes: '',
  }
}

function createDefaultGroupForm(): ProxyGroupForm {
  return {
    id: '',
    name: '',
    enabled: true,
    notes: '',
    nodes: [createDefaultNode(0)],
  }
}

function normalizeGroupNode(item: ProxyNode, index: number): ProxyNode {
  const id = normalizeGroupId(item.id || '') || createGeneratedId('node')
  return {
    id,
    name: String(item.name || `出口 ${index + 1}`).trim(),
    url: String(item.url || '').trim(),
    enabled: item.enabled !== false,
    image_concurrency_limit: normalizeImageConcurrencyLimit(item.image_concurrency_limit ?? DEFAULT_PROXY_NODE_IMAGE_CONCURRENCY),
    last_latency_ms: Number(item.last_latency_ms || 0),
    fail_count: Number(item.fail_count || 0),
    last_error: String(item.last_error || '').trim(),
    last_checked_at: String(item.last_checked_at || '').trim(),
    last_error_at: String(item.last_error_at || '').trim(),
    cooldown_until: String(item.cooldown_until || '').trim(),
    notes: String(item.notes || '').trim(),
  }
}

function normalizeGroup(item: ProxyGroup): ProxyGroup {
  const id = normalizeGroupId(item.id || item.name || '')
  return {
    id,
    name: String(item.name || item.id || '').trim(),
    strategy: item.strategy || 'request_random',
    rotation_interval_minutes: 0,
    enabled: item.enabled !== false,
    notes: String(item.notes || '').trim(),
    nodes: Array.isArray(item.nodes)
      ? item.nodes.map(normalizeGroupNode).filter((node) => node.id)
      : [],
  }
}

export function proxyActionError(action: string, error: unknown) {
  const message = errorMessage(error, '')
  return message ? `${action}：${message}` : action
}

export function useProxyGroupRuntime() {
  const toast = useToast()
  const confirmDialog = useConfirmDialog()
  const savingGroupId = ref('')
  const deletingGroupId = ref('')
  const testingKey = ref('')
  const groupKeyword = ref('')
  const showGroupModal = ref(false)
  const editingGroupId = ref('')
  const groups = ref<ProxyGroup[]>([])
  const testResults = reactive<Record<string, ProxyTestResult>>({})
  const groupForm = reactive<ProxyGroupForm>(createDefaultGroupForm())

  const filteredGroups = computed(() => {
    const query = groupKeyword.value.trim().toLowerCase()
    const rows = [...groups.value].sort((left, right) => (
      (left.name || left.id).localeCompare(right.name || right.id, 'zh-Hans-CN')
    ))
    if (!query) return rows
    return rows.filter((item) => [
      item.id,
      item.name,
      item.notes,
      ...item.nodes.flatMap((node) => [node.id, node.name, node.url, node.notes]),
    ].some((value) => String(value || '').toLowerCase().includes(query)))
  })

  function updateGroups(items: ProxyGroup[]) {
    groups.value = Array.isArray(items) ? items.map(normalizeGroup).filter((item) => item.id) : []
  }

  async function copyText(value: string, message = '已复制') {
    const text = String(value || '').trim()
    if (!text) return
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
      } else {
        const input = document.createElement('textarea')
        input.value = text
        input.setAttribute('readonly', 'readonly')
        input.style.position = 'fixed'
        input.style.opacity = '0'
        document.body.appendChild(input)
        input.select()
        document.execCommand('copy')
        document.body.removeChild(input)
      }
      toast.success(message)
    } catch {
      toast.error('复制失败')
    }
  }

  function copyProxyGroupReference(group: Pick<ProxyGroup, 'id'>) {
    void copyText(proxyGroupReference(group), '代理组引用已复制')
  }

  function resetGroupForm() {
    editingGroupId.value = ''
    Object.assign(groupForm, createDefaultGroupForm())
  }

  function openCreateGroupModal() {
    resetGroupForm()
    showGroupModal.value = true
  }

  function openEditGroupModal(group: ProxyGroup) {
    editingGroupId.value = group.id
    Object.assign(groupForm, {
      id: group.id,
      name: group.name || group.id,
      enabled: group.enabled !== false,
      notes: group.notes || '',
      nodes: group.nodes.length ? group.nodes.map((node, index) => normalizeGroupNode(node, index)) : [createDefaultNode(0)],
    })
    showGroupModal.value = true
  }

  function closeGroupModal() {
    if (savingGroupId.value === FORM_TEST_KEY) return
    showGroupModal.value = false
    resetGroupForm()
  }

  function addGroupNode() {
    groupForm.nodes.push(createDefaultNode(groupForm.nodes.length))
  }

  function removeGroupNode(index: number) {
    if (groupForm.nodes.length <= 1) {
      groupForm.nodes = [createDefaultNode(0)]
      return
    }
    groupForm.nodes.splice(index, 1)
  }

  async function saveProxyGroup() {
    const groupName = groupForm.name.trim()
    if (!groupName) {
      toast.warning('请填写代理组名称')
      return
    }
    const id = normalizeGroupId(editingGroupId.value || groupForm.id) || createGeneratedId('pg')
    const nodes = groupForm.nodes
      .map((node, index) => normalizeGroupNode(node, index))
      .filter((node) => node.url)
    if (!nodes.length) {
      toast.warning('请至少填写一个代理节点地址')
      return
    }

    savingGroupId.value = FORM_TEST_KEY
    try {
      const wasEditing = Boolean(editingGroupId.value)
      const response = await proxyApi.saveGroup({
        id,
        name: groupName,
        strategy: 'request_random',
        enabled: groupForm.enabled,
        notes: groupForm.notes.trim(),
        nodes,
        create_only: !editingGroupId.value,
      })
      updateGroups(response.groups || [])
      savingGroupId.value = ''
      closeGroupModal()
      toast.success(wasEditing ? '代理组已更新' : '代理组已创建')
    } catch (error) {
      toast.error(proxyActionError('保存代理组失败', error))
    } finally {
      savingGroupId.value = ''
    }
  }

  async function toggleProxyGroup(group: ProxyGroup) {
    const nextEnabled = !group.enabled
    const confirmed = await confirmDialog.ask({
      title: nextEnabled ? '确认启用代理组' : '确认停用代理组',
      message: `即将${nextEnabled ? '启用' : '停用'}代理组 ${group.name || group.id}。绑定到该组的账号组会受到影响，是否继续？`,
      confirmText: nextEnabled ? '启用' : '停用',
      cancelText: '取消',
    })
    if (!confirmed) return

    savingGroupId.value = group.id
    try {
      const response = await proxyApi.saveGroup({
        ...group,
        enabled: nextEnabled,
      })
      updateGroups(response.groups || [])
      toast.success(`代理组 ${group.name || group.id} 已${group.enabled ? '停用' : '启用'}`)
    } catch (error) {
      toast.error(proxyActionError('切换代理组失败', error))
    } finally {
      savingGroupId.value = ''
    }
  }

  async function deleteProxyGroup(group: ProxyGroup) {
    const confirmed = await confirmDialog.ask({
      title: '删除代理组',
      message: `确认删除代理组 ${group.name || group.id}？账号组里已有的绑定不会自动清空。`,
      confirmText: '确认删除',
      cancelText: '取消',
    })
    if (!confirmed) return

    deletingGroupId.value = group.id
    try {
      const response = await proxyApi.deleteGroup(group.id)
      updateGroups(response.groups || [])
      toast.success('代理组已删除')
    } catch (error) {
      toast.error(proxyActionError('删除代理组失败', error))
    } finally {
      deletingGroupId.value = ''
    }
  }

  function proxyGroupActionItems(group: ProxyGroup) {
    return buildProxyGroupActionItems(group, testingKey.value, savingGroupId.value, deletingGroupId.value)
  }

  function handleProxyGroupAction(group: ProxyGroup, action: string) {
    if (action === 'test-all') void testProxyGroupAll(group)
    if (action === 'toggle-enabled') void toggleProxyGroup(group)
    if (action === 'delete') void deleteProxyGroup(group)
  }

  async function testProxyGroupNode(group: Pick<ProxyGroup, 'id' | 'name'>, node: ProxyNode) {
    const confirmed = await confirmDialog.ask({
      title: '确认测试代理节点',
      message: `即将使用代理组 ${group.name || group.id} 的节点 ${node.name || node.id} 发起外部网络测试请求。请确认当前允许测试该代理连接。`,
      confirmText: '开始测试',
      cancelText: '取消',
    })
    if (!confirmed) return

    const key = `group:${group.id}:${node.id}`
    testingKey.value = key
    try {
      const response = await proxyApi.testGroup({ id: group.id, node_id: node.id })
      if (response.groups) updateGroups(response.groups)
      const result = response.result || response.results?.[0]?.result
      if (result) testResults[key] = result
      if (result?.ok) toast.success(`节点检测通过，耗时 ${result.latency_ms}ms`)
      else toast.warning(result?.error || '节点检测失败')
    } catch (error) {
      const message = errorMessage(error, '节点检测失败')
      testResults[key] = {
        ok: false,
        status: 0,
        latency_ms: 0,
        error: message,
      }
      toast.error(message)
    } finally {
      testingKey.value = ''
    }
  }

  async function testProxyGroupAll(group: ProxyGroup) {
    const confirmed = await confirmDialog.ask({
      title: '确认测试代理组',
      message: `即将测试代理组 ${group.name || group.id} 内的 ${group.nodes.length} 个节点。每个节点都会发起外部网络测试请求，是否继续？`,
      confirmText: '开始测试',
      cancelText: '取消',
    })
    if (!confirmed) return

    const key = `group:${group.id}:all`
    testingKey.value = key
    try {
      const response = await proxyApi.testGroup({ id: group.id })
      if (response.groups) updateGroups(response.groups)
      const results = response.results || []
      for (const item of results) {
        if (item.node_id && item.result) {
          testResults[`group:${group.id}:${item.node_id}`] = item.result
        }
      }
      const failed = results.filter((item) => !item.result.ok)
      if (failed.length) toast.warning(`代理组检测完成，失败 ${failed.length} 个节点`)
      else toast.success(`代理组检测通过，共 ${results.length} 个节点`)
    } catch (error) {
      toast.error(errorMessage(error, '代理组检测失败'))
    } finally {
      testingKey.value = ''
    }
  }

  function nodeTestSummary(group: ProxyGroup, node: ProxyNode) {
    return proxyNodeTestSummary(group, node, testResults, testingKey.value)
  }

  function nodeTestClass(group: ProxyGroup, node: ProxyNode) {
    return proxyNodeTestClass(group, node, testResults, testingKey.value)
  }

  return {
    savingGroupId,
    deletingGroupId,
    testingKey,
    groupKeyword,
    showGroupModal,
    editingGroupId,
    groups,
    testResults,
    groupForm,
    filteredGroups,
    updateGroups,
    proxyActionError,
    normalizeGroupId,
    normalizeImageConcurrencyLimit,
    copyProxyGroupReference,
    openCreateGroupModal,
    openEditGroupModal,
    closeGroupModal,
    addGroupNode,
    removeGroupNode,
    saveProxyGroup,
    proxyGroupActionItems,
    handleProxyGroupAction,
    testProxyGroupNode,
    testProxyGroupAll,
    nodeTestSummary,
    nodeTestClass,
  }
}
