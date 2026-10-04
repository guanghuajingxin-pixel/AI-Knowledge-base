import { ref } from 'vue'
import { login as passwordLogin, type Identity } from '../api/chat'
import { createSsoClient, type AuthConfig } from './sso-client'

export const sso = createSsoClient('portal')
export const authConfig = ref<AuthConfig | null>(null)
export const authError = ref<string>('')
export const identity = ref<Identity | null>(null)
export const isAuthReady = ref<boolean>(false)

export async function initializeAuth() {
  isAuthReady.value = false
  authError.value = ''
  try {
    authConfig.value = await sso.init()
    // Remove the obsolete browser-only gate. It is never authentication evidence.
    localStorage.removeItem('jack-aigc-portal.session')
    if (sso.isSso) {
      sso.subscribe(token => {
        if (!token) identity.value = null
        else if (identity.value) identity.value.access_token = token
      })
      const token = await sso.getToken()
      if (token) {
        const response = await fetch('/api/v1/users/me', { headers: { Authorization: `Bearer ${token}` } })
        if (!response.ok) {
          const body = await response.json().catch(() => null) as { detail?: string } | null
          throw new Error(body?.detail || '统一账号尚未获得访问权限，请联系管理员。')
        }
        identity.value = { access_token: token, user: await response.json() }
      }
    }
  } catch (error) { authError.value = error instanceof Error ? error.message : '登录服务不可用。' }
  finally { isAuthReady.value = true }
}
export async function signIn(username: string, password: string) {
  if (!authConfig.value) throw new Error('登录服务尚未就绪，请先重试连接。')
  if (sso.isSso) return sso.login()
  identity.value = await passwordLogin(username, password)
}
export async function accessToken(): Promise<string> {
  return sso.isSso ? sso.getToken() : identity.value?.access_token || ''
}
export async function signOut() {
  identity.value = null
  if (sso.isSso) await sso.logout()
}
