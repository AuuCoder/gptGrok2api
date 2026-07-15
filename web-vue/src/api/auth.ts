import apiClient, { clearAuthToken, setAuthToken } from './client'
import type { AuthStatusResponse, LoginRequest, LoginResponse } from '@/types/api'

export const authApi = {
  async login(data: LoginRequest) {
    setAuthToken(data.password)
    try {
      return await apiClient.post<never, LoginResponse>('/auth/login')
    } catch (error) {
      clearAuthToken()
      throw error
    }
  },

  logout: () => {
    clearAuthToken()
    return Promise.resolve({ ok: true })
  },

  checkAuth: () => apiClient.get<never, AuthStatusResponse>('/auth/status', { timeout: 8000 }),
}
