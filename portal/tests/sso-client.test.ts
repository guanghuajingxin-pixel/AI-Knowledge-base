import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createSsoClient } from '../src/auth/sso-client'

const adapter = vi.hoisted(() => ({
  authenticated: true, token: 'short-lived-access-token', init: vi.fn(), updateToken: vi.fn(), login: vi.fn(),
  clearToken: vi.fn(), createLogoutUrl: vi.fn(() => 'https://sso.test/logout'),
  onAuthLogout: undefined as (() => void) | undefined,
  onAuthRefreshSuccess: undefined as (() => void) | undefined,
  onAuthRefreshError: undefined as (() => void) | undefined,
}))
vi.mock('keycloak-js', () => ({ default: vi.fn(() => adapter) }))
const config = { provider: 'keycloak', url: 'https://sso.test', realm: 'jack', portal_client_id: 'jack-portal', web_client_id: 'knowledge-web' }
beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  adapter.authenticated = true
  adapter.token = 'short-lived-access-token'
  adapter.init.mockResolvedValue(true)
  adapter.updateToken.mockResolvedValue(false)
  const page = { isSecureContext: true, self: {}, top: {} }
  page.top = page.self
  vi.stubGlobal('window', page)
  vi.stubGlobal('location', { origin: 'https://portal.test', pathname: '/', search: '', hash: '', assign: vi.fn() })
  vi.stubGlobal('sessionStorage', { getItem: vi.fn(() => null), setItem: vi.fn(), removeItem: vi.fn() })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(config))))
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })
describe('unified login protocol', () => {
  it('uses code flow + S256 and deduplicates initialization', async () => {
    const client = createSsoClient('portal')
    await Promise.all([client.init(), client.init()])
    expect(adapter.init).toHaveBeenCalledTimes(1)
    expect(adapter.init).toHaveBeenCalledWith(expect.objectContaining({ flow: 'standard', pkceMethod: 'S256', responseMode: 'query' }))
  })
  it('does not silently downgrade when configuration is unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 503 })))
    await expect(createSsoClient('portal').init()).rejects.toThrow('无法获取')
    expect(adapter.init).not.toHaveBeenCalled()
  })
  it('rejects login return addresses outside the app', async () => {
    await expect(createSsoClient('portal').login('https://attacker.test')).rejects.toThrow('返回地址无效')
    expect(adapter.login).not.toHaveBeenCalled()
  })
  it('clears authentication after refresh failure', async () => {
    const client = createSsoClient('portal')
    await client.init()
    adapter.updateToken.mockRejectedValue(new Error('invalid_grant'))
    await expect(client.getToken()).rejects.toThrow('已过期')
    expect(adapter.clearToken).toHaveBeenCalled()
  })
  it('shares one in-flight refresh across parallel requests', async () => {
    const client = createSsoClient('web')
    await client.init()
    const tokens = await Promise.all([client.getToken(), client.getToken()])
    expect(tokens).toEqual(['short-lived-access-token', 'short-lived-access-token'])
    expect(adapter.updateToken).toHaveBeenCalledTimes(1)
  })
  it('refuses insecure origins before invoking the adapter', async () => {
    vi.stubGlobal('window', { isSecureContext: false })
    await expect(createSsoClient('portal').init()).rejects.toThrow('HTTPS')
    expect(adapter.init).not.toHaveBeenCalled()
  })
})
