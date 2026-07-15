import type { ActionMenuItem } from 'nanocat-ui'
import { parseProxyReference, serializeProxyReference, type ProxyGroup, type ProxyNode, type ProxyTestResult } from '@/api/proxy'
import { actionMenuGroups } from '@/components/ai/menuItems'

export type DefaultProxyMode = 'direct' | 'group' | 'custom'
export type FallbackProxyMode = 'off' | DefaultProxyMode

export const defaultProxyModeOptions = [
  { label: '直连', value: 'direct' },
  { label: '代理组', value: 'group' },
  { label: '自定义代理', value: 'custom' },
] as const

export const fallbackProxyModeOptions = [
  { label: '关闭', value: 'off' },
  { label: '直连', value: 'direct' },
  { label: '代理组', value: 'group' },
  { label: '自定义代理', value: 'custom' },
] as const

const defaultProxyModes = new Set<string>(defaultProxyModeOptions.map((item) => item.value))
const fallbackProxyModes = new Set<string>(fallbackProxyModeOptions.map((item) => item.value))

export function toDefaultProxyMode(value: string | string[]): DefaultProxyMode {
  const raw = Array.isArray(value) ? value[0] : value
  return defaultProxyModes.has(raw) ? raw as DefaultProxyMode : 'direct'
}

export function toFallbackProxyMode(value: string | string[]): FallbackProxyMode {
  const raw = Array.isArray(value) ? value[0] : value
  return fallbackProxyModes.has(raw) ? raw as FallbackProxyMode : 'off'
}

export function proxyGroupReference(group: Pick<ProxyGroup, 'id'>) {
  return serializeProxyReference('group', group.id)
}

function signatureValue(value: unknown): string {
  return String(value ?? '').trim().replaceAll('|', '/')
}

function boundedSignatureText(value: unknown, limit = 160): string {
  const text = signatureValue(value)
  if (text.length <= limit) return text
  return `${text.length}:${text.slice(0, limit)}:${text.slice(-24)}`
}

function proxyNodeSignature(node: ProxyNode) {
  return [
    node.id,
    node.name,
    boundedSignatureText(node.url, 96),
    node.enabled !== false ? 1 : 0,
    node.image_concurrency_limit,
    node.last_latency_ms,
    node.fail_count,
    boundedSignatureText(node.last_error),
    node.last_checked_at,
    node.last_error_at,
    node.cooldown_until,
    boundedSignatureText(node.notes),
  ].map(signatureValue).join(',')
}

function testingKeyForGroup(group: ProxyGroup, testingKey: string) {
  if (testingKey === `group:${group.id}:all`) return testingKey
  return group.nodes.some((node) => testingKey === proxyNodeTestKey(group, node)) ? testingKey : ''
}

export function proxyGroupRowSignature(group: ProxyGroup, testingKey: string, savingGroupId: string, deletingGroupId: string) {
  return [
    group.id,
    group.name,
    group.enabled !== false ? 1 : 0,
    group.strategy,
    group.rotation_interval_minutes,
    boundedSignatureText(group.notes),
    group.nodes.map(proxyNodeSignature).join(';'),
    testingKeyForGroup(group, testingKey),
    savingGroupId === group.id ? savingGroupId : '',
    deletingGroupId === group.id ? deletingGroupId : '',
  ].map(signatureValue).join('|')
}

export function proxyGroupActionItems(
  group: ProxyGroup,
  testingKey: string,
  savingGroupId: string,
  deletingGroupId: string,
): ActionMenuItem[] {
  const allKey = `group:${group.id}:all`
  return actionMenuGroups(
    [
      {
        key: 'test-all',
        label: testingKey === allKey ? '检测中...' : '检测全部节点',
        disabled: testingKey === allKey || group.nodes.length === 0,
      },
    ],
    [
      {
        key: 'toggle-enabled',
        label: savingGroupId === group.id
          ? '处理中...'
          : group.enabled ? '停用代理组' : '启用代理组',
        disabled: savingGroupId === group.id,
      },
    ],
    [
      {
        key: 'delete',
        label: deletingGroupId === group.id ? '删除中...' : '删除代理组',
        danger: true,
        disabled: deletingGroupId === group.id,
      },
    ],
  )
}

export function proxyGroupOptions(
  groups: readonly ProxyGroup[],
  selectedId = '',
) {
  const rows = groups.map((group) => ({
    label: `${group.enabled === false ? '停用 · ' : ''}${group.name || group.id}${Array.isArray(group.nodes) ? ` · ${group.nodes.length} 个节点` : ''}`,
    value: group.id,
  }))
  if (selectedId && !rows.some((item) => item.value === selectedId)) {
    rows.unshift({ label: `未知代理组 · ${selectedId}`, value: selectedId })
  }
  return [
    { label: '选择代理组', value: '' },
    ...rows,
  ]
}

export function buildProxyPreview(
  mode: FallbackProxyMode,
  selectedGroupId: string,
  customInput: string,
  groups: readonly Pick<ProxyGroup, 'id' | 'name'>[],
) {
  if (mode === 'off') return '关闭'
  if (mode === 'direct') return '直连'
  if (mode === 'group') {
    const group = groups.find((item) => item.id === selectedGroupId)
    return selectedGroupId ? `代理组：${group?.name || selectedGroupId}` : '代理组：未选择'
  }
  return customInput || '自定义代理：未填写'
}

export function normalizeDefaultProxyForCompare(value: unknown) {
  const reference = parseProxyReference(value)
  if (reference.mode === 'global' || reference.mode === 'direct') return 'direct'
  if (reference.mode === 'group') return serializeProxyReference('group', reference.value)
  if (reference.mode === 'profile') return String(value || '').trim()
  return reference.value.trim()
}

export function proxyNodeTestKey(group: Pick<ProxyGroup, 'id'>, node: Pick<ProxyNode, 'id'>) {
  return `group:${group.id}:${node.id}`
}

export function isProxyNodeTesting(group: Pick<ProxyGroup, 'id'>, node: Pick<ProxyNode, 'id'>, testingKey: string) {
  return testingKey === `group:${group.id}:all` || testingKey === proxyNodeTestKey(group, node)
}

export function proxyNodeTestSummary(
  group: Pick<ProxyGroup, 'id'>,
  node: ProxyNode,
  testResults: Readonly<Record<string, ProxyTestResult>>,
  testingKey: string,
) {
  if (isProxyNodeTesting(group, node, testingKey)) return '检测中...'
  const result = testResults[proxyNodeTestKey(group, node)]
  if (result?.ok) return `HTTP ${result.status || '-'} · ${result.latency_ms || 0}ms`
  if (result && !result.ok) return result.error || '检测失败'
  if (node.last_error) return node.last_error
  if (node.last_checked_at) return `${node.last_latency_ms || 0}ms`
  return '尚未测试'
}

export function proxyNodeTestClass(
  group: Pick<ProxyGroup, 'id'>,
  node: ProxyNode,
  testResults: Readonly<Record<string, ProxyTestResult>>,
  testingKey: string,
) {
  if (isProxyNodeTesting(group, node, testingKey)) return 'text-sky-600'
  const result = testResults[proxyNodeTestKey(group, node)]
  if (result) return result.ok ? 'text-emerald-600' : 'text-rose-600'
  if (node.last_error) return 'text-rose-600'
  if (node.last_checked_at) return 'text-emerald-600'
  return 'text-muted-foreground'
}
