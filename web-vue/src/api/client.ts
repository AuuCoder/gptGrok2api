import axios, { type AxiosError, type AxiosInstance } from 'axios'
import { errorMessage as formatErrorMessage } from '@/lib/errorMessage'

export const AUTH_TOKEN_STORAGE_KEY = 'chatgpt2api.adminKey'

export function getAuthToken() {
  return window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY) || ''
}

export function setAuthToken(token: string) {
  const cleanToken = token.trim()
  if (cleanToken) {
    window.localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, cleanToken)
    return
  }
  window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY)
}

export function clearAuthToken() {
  window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY)
}

type UnauthorizedHandler = () => void | Promise<void>

let unauthorizedHandler: UnauthorizedHandler | null = null

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null) {
  unauthorizedHandler = handler
}

export const apiClient: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '',
  timeout: 60000,
})

let isRedirectingToLogin = false

apiClient.interceptors.request.use(
  (config) => {
    const token = getAuthToken()
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error),
)

apiClient.interceptors.response.use(
  (response) => response.data,
  (error: AxiosError) => {
    const status = error.response?.status
    const requestUrl = String(error.config?.url || '')
    const isAuthProbe = requestUrl.includes('/auth/status')
    const isLoginRequest = requestUrl.includes('/auth/login')
    const isICloudSidecarResponse = error.response?.headers?.['x-icloud-privacy-mail-proxy'] === '1'

    if (status === 401 && !isLoginRequest && !isAuthProbe && !isICloudSidecarResponse) {
      const onLoginPage = window.location.hash.startsWith('#/login')
      if (!onLoginPage && !isRedirectingToLogin) {
        isRedirectingToLogin = true
        clearAuthToken()
        Promise.resolve(unauthorizedHandler?.())
          .catch(() => {})
          .finally(() => {
            window.setTimeout(() => {
              isRedirectingToLogin = false
            }, 200)
          })
      }
    }

    const errorMessage = formatErrorMessage(error.response?.data || error.message, { fallback: error.message, status })

    const wrapped = new Error(errorMessage || 'Request failed') as Error & {
      status?: number
      data?: unknown
    }
    wrapped.status = error.response?.status
    wrapped.data = error.response?.data
    return Promise.reject(wrapped)
  },
)

export default apiClient
