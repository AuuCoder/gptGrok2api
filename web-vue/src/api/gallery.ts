import apiClient from './client'

export type GalleryMediaType = 'all' | 'image' | 'video' | 'music'

export interface GalleryFile {
  filename: string
  path: string
  url: string
  thumbnail_url: string
  size: number
  created_at: string
  mtime: number
  date: string
  type: Exclude<GalleryMediaType, 'all'>
  expired: boolean
  expires_in_seconds: number | null
  tags: string[]
  storage: string
  local: boolean
  webdav: boolean
  width: number | null
  height: number | null
}

export interface GalleryResponse {
  files: GalleryFile[]
  total: number
  total_size: number
  retention_days: number
  counts: {
    all: number
    image: number
    video: number
    music: number
  }
  media_type: GalleryMediaType
  page: number
  page_size: number
  page_count: number
}

export interface ImageStorageStats {
  disk_total_mb: number
  disk_used_mb: number
  disk_free_mb: number
  image_count: number
  image_size_mb: number
  image_size_bytes: number
}

export interface ImageCompressResult {
  compressed: number
  saved_bytes: number
  saved_mb: number
}

export interface ImageCleanupTargetResult {
  removed: number
  freed_mb?: number
  target_free_mb: number
  current_free_mb: number
  done: boolean
  dry_run?: boolean
}

type BackendImageItem = Record<string, any>

type BackendImagesResponse = {
  items?: BackendImageItem[]
  total?: number
  total_size?: number
  retention_days?: number
  counts?: Partial<GalleryResponse['counts']>
  limit?: number
  offset?: number
  page?: number
  page_size?: number
  page_count?: number
  has_more?: boolean
}

type BackendTagsResponse = {
  tags?: string[]
}

type BackendSettingsResponse = {
  config?: {
    image_retention_days?: number
    basic?: {
      image_expire_hours?: number
    }
  }
}

type GalleryParams = {
  page?: number
  page_size?: number
  media_type?: GalleryMediaType
  tag?: string
  search?: string
  start_date?: string
  end_date?: string
}

const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'])
const VIDEO_EXTENSIONS = new Set(['.mp4', '.webm', '.mov', '.m4v'])
const MUSIC_EXTENSIONS = new Set(['.mp3', '.wav', '.ogg', '.m4a', '.flac', '.vtt', '.lrc', '.txt'])

function cleanString(value: unknown): string {
  return String(value || '').trim()
}

function extensionOf(name: string): string {
  const index = name.lastIndexOf('.')
  return index >= 0 ? name.slice(index).toLowerCase() : ''
}

function mediaTypeFor(item: BackendImageItem): GalleryFile['type'] {
  const explicit = cleanString(item.type).toLowerCase()
  if (explicit === 'video' || explicit === 'music' || explicit === 'image') return explicit
  const ext = extensionOf(cleanString(item.name || item.path || item.rel))
  if (VIDEO_EXTENSIONS.has(ext)) return 'video'
  if (MUSIC_EXTENSIONS.has(ext)) return 'music'
  if (IMAGE_EXTENSIONS.has(ext)) return 'image'
  return 'image'
}

function parseTimeMs(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value > 10_000_000_000 ? Math.floor(value) : Math.floor(value * 1000)
  }
  const raw = cleanString(value)
  if (!raw) return 0
  const parsed = Date.parse(raw.replace(' ', 'T'))
  return Number.isNaN(parsed) ? 0 : parsed
}

function normalizeTags(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return Array.from(new Set(value.map((item) => cleanString(item)).filter(Boolean)))
}

function relativeUrl(value: string, fallbackPath: string) {
  const raw = cleanString(value)
  if (!raw) return `/images/${encodeURI(fallbackPath)}`
  if (raw.startsWith('/')) return raw
  try {
    const parsed = new URL(raw)
    return `${parsed.pathname}${parsed.search}${parsed.hash}`
  } catch {
    return raw.startsWith('images/') || raw.startsWith('image-thumbnails/') ? `/${raw}` : raw
  }
}

function defaultFileBaseUrl(): string {
  if (import.meta.env.VITE_API_URL) return String(import.meta.env.VITE_API_URL)
  if (typeof window !== 'undefined') return window.location.origin
  return ''
}

export function resolveGalleryFileUrl(url: string, baseUrl = defaultFileBaseUrl()): string {
  const raw = cleanString(url)
  if (!raw) return ''
  if (/^[a-z][a-z0-9+.-]*:/i.test(raw)) return raw
  if (raw.startsWith('//')) {
    const protocol = typeof window !== 'undefined' ? window.location.protocol : 'https:'
    return `${protocol}${raw}`
  }
  if (!baseUrl) return raw
  const cleanBase = baseUrl.replace(/\/+$/, '')
  const cleanPath = raw.startsWith('/') ? raw : `/${raw.replace(/^\/+/, '')}`
  return `${cleanBase}${cleanPath}`
}

function expiryFor(createdAtMs: number, retentionDays: number) {
  if (!createdAtMs) {
    return { expired: false, expires_in_seconds: null }
  }
  const expiresAt = createdAtMs + retentionDays * 86400 * 1000
  const remaining = Math.floor((expiresAt - Date.now()) / 1000)
  return {
    expired: remaining <= 0,
    expires_in_seconds: Math.max(0, remaining),
  }
}

function expiryForItem(item: BackendImageItem, createdAtMs: number, retentionDays: number) {
  if (typeof item.expired === 'boolean' || item.expires_in_seconds !== undefined) {
    const remaining = item.expires_in_seconds === null || item.expires_in_seconds === undefined
      ? null
      : Math.max(0, Number(item.expires_in_seconds) || 0)
    return {
      expired: Boolean(item.expired),
      expires_in_seconds: remaining,
    }
  }
  return expiryFor(createdAtMs, retentionDays)
}

function mapFile(item: BackendImageItem, retentionDays: number): GalleryFile {
  const path = cleanString(item.path || item.rel || item.name)
  const name = cleanString(item.filename || item.name || path.split('/').pop() || path)
  const createdAt = cleanString(item.created_at)
  const createdAtMs = parseTimeMs(createdAt)
  const expiry = expiryForItem(item, createdAtMs, retentionDays)
  const safePath = path || name
  return {
    filename: name || safePath,
    path: safePath,
    url: relativeUrl(cleanString(item.url), safePath),
    thumbnail_url: relativeUrl(cleanString(item.thumbnail_url || item.url), safePath),
    size: Number(item.size || 0),
    created_at: createdAt,
    mtime: createdAtMs ? Math.floor(createdAtMs / 1000) : 0,
    date: cleanString(item.date),
    type: mediaTypeFor(item),
    expired: expiry.expired,
    expires_in_seconds: expiry.expires_in_seconds,
    tags: normalizeTags(item.tags),
    storage: cleanString(item.storage || (item.webdav ? (item.local ? 'both' : 'webdav') : 'local')),
    local: Boolean(item.local ?? true),
    webdav: Boolean(item.webdav ?? false),
    width: Number.isFinite(Number(item.width)) ? Number(item.width) : null,
    height: Number.isFinite(Number(item.height)) ? Number(item.height) : null,
  }
}

function paginate<T>(items: T[], page: number, pageSize: number) {
  const safePageSize = Math.min(Math.max(Number(pageSize || 24), 1), 200)
  const pageCount = Math.max(1, Math.ceil(items.length / safePageSize))
  const safePage = Math.min(Math.max(Number(page || 1), 1), pageCount)
  const start = (safePage - 1) * safePageSize
  return {
    items: items.slice(start, start + safePageSize),
    page: safePage,
    pageSize: safePageSize,
    pageCount,
  }
}

function countByType(files: GalleryFile[]) {
  return {
    all: files.length,
    image: files.filter((file) => file.type === 'image').length,
    video: files.filter((file) => file.type === 'video').length,
    music: files.filter((file) => file.type === 'music').length,
  }
}

function matchesSearch(file: GalleryFile, search: string) {
  const keyword = search.trim().toLowerCase()
  if (!keyword) return true
  return [
    file.filename,
    file.path,
    file.created_at,
    file.storage,
    ...file.tags,
  ].some((value) => cleanString(value).toLowerCase().includes(keyword))
}

async function getRetentionDays() {
  try {
    const settings = await apiClient.get<never, BackendSettingsResponse>('/api/settings')
    const value = Number(settings.config?.image_retention_days ?? settings.config?.basic?.image_expire_hours ?? 15)
    return Number.isFinite(value) && value >= 1 ? Math.floor(value) : 15
  } catch {
    return 15
  }
}

type GalleryListParams = GalleryParams & {
  limit?: number
  offset?: number
}

async function listMappedFiles(params?: GalleryListParams) {
  const requestParams: Record<string, string | number> = {}
  if (params?.start_date) requestParams.start_date = params.start_date
  if (params?.end_date) requestParams.end_date = params.end_date
  if (params?.media_type && params.media_type !== 'all') requestParams.media_type = params.media_type
  if (params?.tag && params.tag !== 'all') requestParams.tag = params.tag
  if (params?.search) requestParams.search = params.search
  if (params?.limit !== undefined && Number.isFinite(params.limit)) requestParams.limit = Number(params.limit)
  if (params?.offset !== undefined && Number.isFinite(params.offset)) requestParams.offset = Number(params.offset)

  const images = await apiClient.get<never, BackendImagesResponse>('/api/images', { params: requestParams })
  const responseRetentionDays = Number(images.retention_days)
  const retentionDays = Number.isFinite(responseRetentionDays) && responseRetentionDays >= 1
    ? Math.floor(responseRetentionDays)
    : await getRetentionDays()

  return {
    files: (images.items || []).map((item) => mapFile(item, retentionDays)),
    retentionDays,
    meta: images,
  }
}

export const galleryApi = {
  getFiles: async (params?: GalleryParams): Promise<GalleryResponse> => {
    const mediaType = params?.media_type || 'all'
    const tag = cleanString(params?.tag)
    const search = cleanString(params?.search)
    const pageSize = Math.min(Math.max(Number(params?.page_size || 24), 1), 200)
    const requestedPage = Math.max(Number(params?.page || 1), 1)
    const { files, retentionDays, meta } = await listMappedFiles({
      start_date: params?.start_date,
      end_date: params?.end_date,
      media_type: mediaType,
      tag,
      search,
      limit: pageSize,
      offset: (requestedPage - 1) * pageSize,
    })
    const serverPaged = Number.isFinite(Number(meta.total))
    if (serverPaged) {
      const total = Number(meta.total || 0)
      const responsePageSize = Math.max(Number(meta.page_size || pageSize), 1)
      return {
        files,
        total,
        total_size: Number(meta.total_size || 0),
        retention_days: retentionDays,
        counts: {
          all: Number(meta.counts?.all || 0),
          image: Number(meta.counts?.image || 0),
          video: Number(meta.counts?.video || 0),
          music: Number(meta.counts?.music || 0),
        },
        media_type: mediaType,
        page: Math.max(Number(meta.page || 1), 1),
        page_size: responsePageSize,
        page_count: Math.max(Number(meta.page_count || Math.ceil(total / responsePageSize) || 1), 1),
      }
    }

    const counts = countByType(files)
    const filtered = files.filter((file) => {
      if (mediaType !== 'all' && file.type !== mediaType) return false
      if (tag && tag !== 'all' && !file.tags.includes(tag)) return false
      return matchesSearch(file, search)
    })
    const page = paginate(filtered, requestedPage, pageSize)
    return {
      files: page.items,
      total: filtered.length,
      total_size: filtered.reduce((sum, file) => sum + file.size, 0),
      retention_days: retentionDays,
      counts,
      media_type: mediaType,
      page: page.page,
      page_size: page.pageSize,
      page_count: page.pageCount,
    }
  },

  deleteFiles: (paths: string[]) =>
    apiClient.post<{ paths: string[] }, { removed: number }>('/api/images/delete', {
      paths,
    }),

  deleteFile: (path: string) =>
    galleryApi.deleteFiles([path]),

  downloadZip: (paths: string[]) =>
    apiClient.post<{ paths: string[] }, Blob>('/api/images/download', {
      paths,
    }, {
      responseType: 'blob',
    }),

  getTags: async () => {
    const response = await apiClient.get<never, BackendTagsResponse>('/api/images/tags')
    return Array.from(new Set((response.tags || []).map((tag) => cleanString(tag)).filter(Boolean)))
  },

  updateTags: (path: string, tags: string[]) =>
    apiClient.post<{ path: string; tags: string[] }, { ok: boolean; tags: string[] }>('/api/images/tags', {
      path,
      tags,
    }),

  deleteTag: (tag: string) =>
    apiClient.delete<never, { ok: boolean; removed_from: number }>(`/api/images/tags/${encodeURIComponent(tag)}`),

  getStorage: () =>
    apiClient.get<never, ImageStorageStats>('/api/images/storage'),

  compressStorage: () =>
    apiClient.post<never, ImageCompressResult>('/api/images/storage/compress'),

  cleanupToTarget: (targetFreeMb: number, dryRun = false) =>
    apiClient.post<never, ImageCleanupTargetResult>('/api/images/storage/cleanup-to-target', null, {
      params: {
        target_free_mb: targetFreeMb,
        dry_run: dryRun,
      },
    }),

  cleanupExpired: async () => {
    const { files } = await listMappedFiles({ limit: 0 })
    const expiredPaths = files.filter((file) => file.expired).map((file) => file.path)
    if (!expiredPaths.length) {
      return { success: true, deleted: 0, message: '没有过期图片需要清理。' }
    }
    const result = await galleryApi.deleteFiles(expiredPaths)
    return {
      success: true,
      deleted: Number(result.removed || 0),
      message: `已清理 ${Number(result.removed || 0)} 张过期图片。`,
    }
  },
}
