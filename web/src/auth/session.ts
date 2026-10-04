import { ref } from 'vue'
import { createSsoClient, type AuthConfig } from './sso-client'

export const sso = createSsoClient('web', import.meta.env.VITE_API_BASE_URL || '/api/v1')
export const authConfig = ref<AuthConfig | null>(null)
export const authError = ref<string>('')
export async function initializeAuth() {
  authError.value = ''
  try { authConfig.value = await sso.init() }
  catch (error) { authError.value = error instanceof Error ? error.message : '登录服务不可用。' }
}
