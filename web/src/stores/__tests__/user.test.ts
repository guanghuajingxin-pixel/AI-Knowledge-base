/// <reference types="vitest/globals" />
import { setActivePinia, createPinia } from 'pinia'
import { useUserStore } from '../user'
import { sso } from '@/auth/session'

describe('useUserStore', () => {
  afterEach(() => vi.restoreAllMocks())
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })

  it('初始状态 token 为空', () => {
    const store = useUserStore()
    expect(store.token).toBe('')
    expect(store.userInfo).toBeNull()
  })

  it('setToken 持久化到 localStorage', () => {
    const store = useUserStore()
    store.setToken('abc123')
    expect(store.token).toBe('abc123')
    expect(localStorage.getItem('kb_token')).toBe('abc123')
  })

  it('logout 清除 token 和用户信息', () => {
    const store = useUserStore()
    store.setToken('abc123')
    store.userInfo = { id: '1', username: 'a', email: 'a@b.c', role: 'admin', is_active: true, created_at: '' }
    store.logout()
    expect(store.token).toBe('')
    expect(store.userInfo).toBeNull()
    expect(localStorage.getItem('kb_token')).toBeNull()
  })
  it('统一登录不恢复旧密码令牌，也不持久化新访问令牌', () => {
    localStorage.setItem('kb_token', 'old-local-token')
    vi.spyOn(sso, 'isSso', 'get').mockReturnValue(true)
    const store = useUserStore()
    expect(store.token).toBe('')
    store.setToken('keycloak-access-token')
    store.setUserInfo({ id: '1', username: 'jack', email: '', role: 'viewer', is_active: true, created_at: '' })
    expect(localStorage.getItem('kb_token')).toBeNull()
    expect(localStorage.getItem('kb_user')).toBeNull()
  })

  it('认证失败清理本地状态时不会重复触发统一退出跳转', () => {
    vi.spyOn(sso, 'isSso', 'get').mockReturnValue(true)
    const logout = vi.spyOn(sso, 'logout').mockResolvedValue(undefined)
    const store = useUserStore()
    store.setToken('keycloak-access-token')
    store.logout(false)
    expect(store.token).toBe('')
    expect(logout).not.toHaveBeenCalled()
  })

})
