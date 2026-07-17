import apiClient from './client'
import type { ClearanceTestResult, ProxyRuntimeSettings, ProxyRuntimeStatus } from '@/types/api'

export interface ProxyTestResult {
  ok: boolean
  status: number
  latency_ms: number
  error?: string | null
}

export interface ProxySampleAttempt extends ProxyTestResult {
  scheme: string
}

export interface ProxySampleTestResult extends ProxyTestResult {
  scheme: string
  sample_index: number
  sample_count: number
  attempts: ProxySampleAttempt[]
  normalized_urls?: string
  normalized_changed?: boolean
}

export interface ProxyProfile {
  id: string
  name: string
  proxy: string
  no_proxy?: string
  enabled: boolean
  notes?: string
}

export type ProxyProfilePayload = Partial<ProxyProfile> & {
  create_only?: boolean
}

export interface ProxyNode {
  id: string
  name: string
  url: string
  enabled: boolean
  image_concurrency_limit?: number
  last_latency_ms?: number
  fail_count?: number
  last_error?: string
  last_checked_at?: string
  last_error_at?: string
  cooldown_until?: string
  notes?: string
}

export interface ProxyGroup {
  id: string
  name: string
  strategy: 'request_random' | 'time_window' | 'round_robin'
  rotation_interval_minutes?: number
  enabled: boolean
  notes?: string
  nodes: ProxyNode[]
}

export type ProxyGroupPayload = Partial<ProxyGroup> & {
  create_only?: boolean
}

export type ProxyReferenceMode = 'global' | 'direct' | 'profile' | 'group' | 'custom'

export interface ProxyReference {
  mode: ProxyReferenceMode
  value: string
}

export type { ClearanceTestResult, ProxyRuntimeSettings, ProxyRuntimeStatus }

export function parseProxyReference(value: unknown): ProxyReference {
  const raw = String(value || '').trim()
  const lower = raw.toLowerCase()
  if (!raw) return { mode: 'global', value: '' }
  if (lower === 'direct') return { mode: 'direct', value: '' }
  if (lower.startsWith('profile:')) {
    return { mode: 'profile', value: raw.slice('profile:'.length).trim() }
  }
  if (lower.startsWith('group:')) {
    return { mode: 'group', value: raw.slice('group:'.length).trim() }
  }
  return { mode: 'custom', value: raw }
}

export function serializeProxyReference(mode: ProxyReferenceMode, value = ''): string {
  const raw = String(value || '').trim()
  if (mode === 'global') return ''
  if (mode === 'direct') return 'direct'
  if (mode === 'profile') return raw ? `profile:${raw}` : ''
  if (mode === 'group') return raw ? `group:${raw}` : ''
  return raw
}

export function proxyReferenceLabel(value: unknown): string {
  const reference = parseProxyReference(value)
  if (reference.mode === 'global') return '使用默认出口'
  if (reference.mode === 'direct') return '直连'
  if (reference.mode === 'profile') return `历史代理配置 ${reference.value || '-'}`
  if (reference.mode === 'group') return `代理组 ${reference.value || '-'}`
  return reference.value
}

export const proxyApi = {
  test: (url: string) =>
    apiClient.post<{ url: string }, { result: ProxyTestResult }>('/api/proxy/test', { url }),

  testSample: (urls: string) =>
    apiClient.post<{ urls: string }, { result: ProxySampleTestResult }>('/api/proxy/sample-test', { urls }),

  listProfiles: () =>
    apiClient.get<never, { profiles: ProxyProfile[] }>('/api/proxy/profiles'),

  saveProfile: (payload: ProxyProfilePayload) =>
    apiClient.post<ProxyProfilePayload, { profile: ProxyProfile; profiles: ProxyProfile[] }>(
      '/api/proxy/profiles',
      payload,
    ),

  deleteProfile: (id: string) =>
    apiClient.delete<never, { deleted: string; profiles: ProxyProfile[] }>(
      `/api/proxy/profiles/${encodeURIComponent(id)}`,
    ),

  testProfile: (payload: { id?: string; url?: string }) =>
    apiClient.post<{ id?: string; url?: string }, { result: ProxyTestResult }>(
      '/api/proxy/profiles/test',
      payload,
    ),

  listGroups: () =>
    apiClient.get<never, { groups: ProxyGroup[] }>('/api/proxy/groups'),

  saveGroup: (payload: ProxyGroupPayload) =>
    apiClient.post<ProxyGroupPayload, { group: ProxyGroup; groups: ProxyGroup[] }>(
      '/api/proxy/groups',
      payload,
    ),

  deleteGroup: (id: string) =>
    apiClient.delete<never, { deleted: string; groups: ProxyGroup[] }>(
      `/api/proxy/groups/${encodeURIComponent(id)}`,
    ),

  testGroup: (payload: { id?: string; node_id?: string; url?: string }) =>
    apiClient.post<
      { id?: string; node_id?: string; url?: string },
      { result?: ProxyTestResult | null; results?: Array<{ node_id: string; result: ProxyTestResult }>; groups?: ProxyGroup[] }
    >('/api/proxy/groups/test', payload),

  getRuntime: () =>
    apiClient.get<never, { runtime: ProxyRuntimeSettings; status: ProxyRuntimeStatus }>('/api/proxy/runtime'),

  testClearance: (targetUrl = 'https://chatgpt.com') =>
    apiClient.post<{ target_url: string }, { result: ClearanceTestResult }>(
      '/api/proxy/clearance/test',
      { target_url: targetUrl },
    ),
}
