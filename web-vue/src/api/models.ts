import apiClient from './client'

export interface OpenAIModel {
  id: string
  object?: string
  created?: number
  owned_by?: string
  root?: string
  parent?: string | null
}

export interface ModelListResponse {
  object?: string
  data: OpenAIModel[]
}

export interface ModelCatalogResponse {
  object?: 'model_catalog' | string
  chat_models: string[]
  image_models: string[]
  image_edit_models?: string[]
  video_models?: string[]
  all_models?: string[]
  source?: {
    chat?: string
    image?: string
  }
  openai_models_endpoint?: string
}

export const modelsApi = {
  catalog: () => apiClient.get<never, ModelCatalogResponse>('/api/model-catalog'),
  list: () => apiClient.get<never, ModelListResponse>('/v1/models'),
}
