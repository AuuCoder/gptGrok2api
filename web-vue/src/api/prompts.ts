import axios, { type Method } from 'axios'
import { getAuthToken } from './client'

export interface PromptLibraryItem {
  id: string
  source_id: string
  source_name: string
  source_url: string
  title: string
  description: string
  preview: string
  link: string
  prompt: string
  mode: string
  image_mode: string
  category: string
  sub_category: string
  tags: string[]
  reference_image_urls: string[]
  image_model: string
  image_size: string
  image_count?: number
  enabled: boolean
  sort_order?: number
  created_at: string
  updated_at: string
}

export interface PromptSource {
  id: string
  name: string
  url: string
  adapter: string
  adapter_label: string
  homepage: string
  enabled: boolean
  built_in: boolean
  sort_order?: number
  created_at: string
  updated_at: string
  prompt_count: number
  last_sync_at: string
  last_error: string
  last_fetch_ms?: number
}

export interface PromptSourcePayload {
  enabled?: boolean
}

export interface PromptLibraryResponse {
  items: PromptLibraryItem[]
  prompt_count?: number
  sources: PromptSource[]
  source_count?: number
  synced?: boolean
  cached_source_count?: number
  enabled_source_count?: number
  source_error_count?: number
  source_errors?: Array<{ id: string; name: string; error: string }>
}

export interface PromptSourceResponse {
  sources: PromptSource[]
  source_count?: number
  source?: PromptSource
}

type PromptApiFailure = {
  url: string
  status?: number
  contentType?: string
  reason: string
}

const PROMPT_API_TIMEOUT_MS = 60000

function trimBaseUrl(value: unknown) {
  return String(value || '').replace(/\/+$/, '')
}

function promptApiBaseCandidates(): string[] {
  const configured = trimBaseUrl(import.meta.env.VITE_API_URL)
  const bases = [configured]

  if (import.meta.env.DEV && typeof window !== 'undefined') {
    const { protocol, hostname, port } = window.location
    if (!configured && port && port !== '8000') {
      bases.push(`${protocol}//${hostname}:8000`)
      if (hostname === 'localhost') bases.push(`${protocol}//127.0.0.1:8000`)
      if (hostname === '127.0.0.1') bases.push(`${protocol}//localhost:8000`)
    }
  }

  return Array.from(new Set(bases))
}

function promptApiUrl(baseUrl: string, path: string) {
  return baseUrl ? `${baseUrl}${path}` : path
}

function promptApiPageOrigin() {
  if (typeof window === 'undefined') return ''
  return window.location.origin
}

function describePromptApiFailure(label: string, path: string, failures: PromptApiFailure[]) {
  const last = failures[failures.length - 1]
  const details = last
    ? `请求地址：${last.url}；页面：${promptApiPageOrigin() || '-'}；Content-Type：${last.contentType || '-'}`
    : `请求路径：${path}`
  return `${label} 请求没有拿到 API JSON。${details}。${last?.reason || '请确认前端 API 指向当前后端。'}`
}

function parsePromptApiJson(text: string, label: string, url: string, contentType: string): Record<string, unknown> {
  const payloadText = text.trim()
  if (payloadText.startsWith('<')) {
    throw new Error(`${label} 请求拿到了 HTML 页面，不是 API JSON。请求地址：${url}；Content-Type：${contentType || '-'}`)
  }
  try {
    const parsed = JSON.parse(payloadText)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>
    }
  } catch {
    // handled by the unified error below
  }
  throw new Error(`${label} 返回格式异常，请确认后端已返回 JSON 对象。请求地址：${url}`)
}

function responseErrorMessage(text: string, fallback: string) {
  const payloadText = text.trim()
  if (payloadText.startsWith('{') || payloadText.startsWith('[')) {
    try {
      const parsed = JSON.parse(payloadText)
      const detail = parsed?.detail
      const error = detail?.error || parsed?.error || parsed?.message
      if (error) return String(error)
    } catch {
      // fall through
    }
  }
  return fallback
}

async function requestPromptApi<T extends object>(
  method: Method,
  path: string,
  label: string,
  data?: unknown,
): Promise<T> {
  const token = getAuthToken()
  const failures: PromptApiFailure[] = []

  for (const baseUrl of promptApiBaseCandidates()) {
    const url = promptApiUrl(baseUrl, path)
    try {
      const response = await axios.request<string>({
        method,
        url,
        data,
        timeout: PROMPT_API_TIMEOUT_MS,
        responseType: 'text',
        transformResponse: [(value) => value],
        validateStatus: () => true,
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      })
      const contentType = String(response.headers?.['content-type'] || '')
      const text = String(response.data || '')

      if (response.status < 200 || response.status >= 300) {
        const reason = responseErrorMessage(text, `HTTP ${response.status}`)
        failures.push({ url, status: response.status, contentType, reason })
        if (response.status === 401 || response.status === 403) {
          throw new Error(`${label} 请求失败：${reason}`)
        }
        continue
      }

      if (contentType.includes('text/html') || text.trim().startsWith('<')) {
        failures.push({ url, status: response.status, contentType, reason: '返回的是前端页面 HTML，说明请求被静态页面兜底接走。' })
        continue
      }

      return parsePromptApiJson(text, label, url, contentType) as T
    } catch (error: any) {
      const message = String(error?.message || error || '请求失败')
      if (message.includes('请求失败：') || message.includes('请求拿到了 HTML 页面')) throw error
      failures.push({ url, reason: message })
    }
  }

  throw new Error(describePromptApiFailure(label, path, failures))
}

function cleanText(value: unknown) {
  return String(value || '').trim()
}

function normalizeStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map(cleanText).filter(Boolean)
}

function normalizePrompt(raw: Partial<PromptLibraryItem>): PromptLibraryItem {
  const imageCount = Number(raw.image_count)
  const sortOrder = Number(raw.sort_order)
  return {
    id: cleanText(raw.id),
    source_id: cleanText(raw.source_id),
    source_name: cleanText(raw.source_name),
    source_url: cleanText(raw.source_url),
    title: cleanText(raw.title),
    description: cleanText(raw.description),
    preview: cleanText(raw.preview),
    link: cleanText(raw.link),
    prompt: cleanText(raw.prompt),
    mode: cleanText(raw.mode),
    image_mode: cleanText(raw.image_mode),
    category: cleanText(raw.category),
    sub_category: cleanText(raw.sub_category),
    tags: normalizeStringList(raw.tags),
    reference_image_urls: normalizeStringList(raw.reference_image_urls),
    image_model: cleanText(raw.image_model),
    image_size: cleanText(raw.image_size),
    image_count: Number.isFinite(imageCount) ? imageCount : undefined,
    enabled: raw.enabled !== false,
    sort_order: Number.isFinite(sortOrder) ? sortOrder : undefined,
    created_at: cleanText(raw.created_at),
    updated_at: cleanText(raw.updated_at),
  }
}

function normalizeSource(raw: Partial<PromptSource>): PromptSource {
  const sortOrder = Number(raw.sort_order)
  const lastFetchMs = Number(raw.last_fetch_ms)
  return {
    id: cleanText(raw.id),
    name: cleanText(raw.name),
    url: cleanText(raw.url),
    adapter: cleanText(raw.adapter) || 'json',
    adapter_label: cleanText(raw.adapter_label) || cleanText(raw.adapter) || 'JSON',
    homepage: cleanText(raw.homepage),
    enabled: raw.enabled !== false,
    built_in: Boolean(raw.built_in),
    sort_order: Number.isFinite(sortOrder) ? sortOrder : undefined,
    created_at: cleanText(raw.created_at),
    updated_at: cleanText(raw.updated_at),
    prompt_count: Number(raw.prompt_count) || 0,
    last_sync_at: cleanText(raw.last_sync_at),
    last_error: cleanText(raw.last_error),
    last_fetch_ms: Number.isFinite(lastFetchMs) ? lastFetchMs : undefined,
  }
}

function coercePlainObject(value: unknown, label: string): Record<string, unknown> {
  if (typeof value === 'string') {
    const text = value.trim()
    if (text.startsWith('{') || text.startsWith('[')) {
      try {
        return coercePlainObject(JSON.parse(text), label)
      } catch {
        throw new Error(`${label} 返回了无法解析的 JSON 字符串，请确认后端响应格式。`)
      }
    }
    if (text.startsWith('<')) {
      throw new Error(`${label} 请求拿到了 HTML 页面，不是 API JSON；请确认前端 API 指向当前后端。`)
    }
  }
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  throw new Error(`${label} 返回格式异常，请确认后端已重启并返回 JSON 数据。`)
}

function normalizeLibraryResponse(response: Partial<PromptLibraryResponse>): PromptLibraryResponse {
  const payload = coercePlainObject(response, '提示词库')
  if (!Array.isArray(payload.items) || !Array.isArray(payload.sources)) {
    throw new Error('提示词库返回缺少 items 或 sources 字段，请确认后端版本已更新。')
  }
  const rawItems = Array.isArray(response.items) ? response.items : []
  const items = rawItems.map(normalizePrompt).filter((item) => item.id && item.title && item.prompt)
  const sources = Array.isArray(response.sources)
    ? response.sources.map(normalizeSource).filter((source) => source.id && source.url)
    : []
  return {
    ...response,
    items,
    sources,
    prompt_count: Number.isFinite(Number(response.prompt_count)) ? Number(response.prompt_count) : items.length,
    source_count: Number.isFinite(Number(response.source_count)) ? Number(response.source_count) : sources.length,
    synced: Boolean(response.synced),
    cached_source_count: Number(response.cached_source_count) || 0,
    enabled_source_count: Number(response.enabled_source_count) || 0,
    source_error_count: Number(response.source_error_count) || 0,
    source_errors: Array.isArray(response.source_errors) ? response.source_errors : [],
  }
}

function normalizeSourceResponse(response: Partial<PromptSourceResponse>): PromptSourceResponse {
  const payload = coercePlainObject(response, '提示词源')
  if (!Array.isArray(payload.sources)) {
    throw new Error('提示词源返回缺少 sources 字段，请确认后端版本已更新。')
  }
  const sources = Array.isArray(response.sources)
    ? response.sources.map(normalizeSource).filter((source) => source.id && source.url)
    : []
  return {
    ...response,
    sources,
    source: response.source ? normalizeSource(response.source) : undefined,
    source_count: Number.isFinite(Number(response.source_count)) ? Number(response.source_count) : sources.length,
  }
}

export const promptsApi = {
  list: async () => normalizeLibraryResponse(await requestPromptApi<PromptLibraryResponse>('GET', '/api/prompts', '提示词库')),
  listSources: async () => normalizeSourceResponse(await requestPromptApi<PromptSourceResponse>('GET', '/api/admin/prompt-sources', '提示词源')),
  updateSource: async (id: string, payload: PromptSourcePayload) =>
    normalizeSourceResponse(await requestPromptApi<PromptSourceResponse>('POST', `/api/admin/prompt-sources/${encodeURIComponent(id)}`, '提示词源', payload)),
  refreshSource: async (id: string) =>
    normalizeLibraryResponse(await requestPromptApi<PromptLibraryResponse>('POST', `/api/admin/prompt-sources/${encodeURIComponent(id)}/refresh`, '提示词源更新')),
  refreshSources: async () =>
    normalizeLibraryResponse(await requestPromptApi<PromptLibraryResponse>('POST', '/api/admin/prompt-sources/refresh', '提示词源更新')),
}
