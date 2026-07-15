import apiClient from './client'

export type DebugSearchSource = {
  title?: string
  url?: string
  snippet?: string
  source_type?: string
}

export type DebugSearchImageGroup = {
  queries?: string[]
  aspect_ratio?: string
  num_per_query?: number
}

export type DebugSearchResult = {
  conversation_id?: string
  status?: string
  answer?: string
  sources?: DebugSearchSource[]
  image_groups?: DebugSearchImageGroup[]
}

export type DebugChatContentPart =
  | { type: 'text'; text: string }
  | { type: 'image_url'; image_url: { url: string } }
  | { type: 'input_image'; image_url?: string; url?: string }

export type DebugChatMessage = {
  role: 'system' | 'user' | 'assistant'
  content: string | DebugChatContentPart[]
}

export type DebugChatCompletion = {
  choices?: Array<{ message?: { role?: string; content?: string } }>
  [key: string]: unknown
}

export type DebugEditableKind = 'ppt' | 'psd'

export type DebugEditableFileTask = {
  id?: string
  taskId?: string
  status?: 'queued' | 'running' | 'success' | 'error' | string
  kind?: DebugEditableKind | string
  created_at?: string
  updated_at?: string
  elapsed_seconds?: number
  prompt_preview?: string
  error?: string
  result?: {
    conversation_id?: string
    primary_url?: string
    zip_url?: string
    [key: string]: unknown
  }
  [key: string]: unknown
}

export type DebugEditableTasksResponse = {
  items: DebugEditableFileTask[]
  missing_ids?: string[]
}

export function createDebugClientTaskId(prefix = 'debug') {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `${prefix}-${crypto.randomUUID()}`
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function endpointForEditableKind(kind: DebugEditableKind) {
  return kind === 'psd' ? '/v1/psd/generations' : '/v1/ppt/generations'
}

export const debugApi = {
  search: async (prompt: string) => apiClient.post<{ prompt: string }, DebugSearchResult>('/v1/search', { prompt }),

  chat: async (model: string, messages: DebugChatMessage[], reasoningEffort = '') => apiClient.post<Record<string, unknown>, DebugChatCompletion>(
    '/v1/chat/completions',
    {
      model: model.trim() || 'auto',
      messages,
      ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}),
    },
  ),

  createEditableFileTask: async (kind: DebugEditableKind, input: { prompt: string; base64_images?: string[] }) => {
    const payload = {
      client_task_id: createDebugClientTaskId(kind),
      prompt: input.prompt,
      base64_images: input.base64_images || [],
    }
    return apiClient.post<typeof payload, DebugEditableFileTask>(endpointForEditableKind(kind), payload)
  },

  listEditableFileTasks: async (ids?: string[]) => {
    const params = ids?.length ? { ids: ids.join(',') } : undefined
    return apiClient.get<never, DebugEditableTasksResponse>('/v1/editable-file-tasks', { params })
  },
}
