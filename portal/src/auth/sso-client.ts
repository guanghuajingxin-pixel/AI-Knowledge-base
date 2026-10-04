import Keycloak from 'keycloak-js'

export interface AuthConfig {
  provider: 'local' | 'keycloak'
  url?: string
  realm?: string
  portal_client_id?: string
  web_client_id?: string
  account_url?: string
  admin_url?: string
}

type TokenListener = (token: string) => void
/** Code + PKCE. Tokens stay in the adapter's memory; only return paths use sessionStorage. */
export function createSsoClient(application: 'portal' | 'web', apiBase = '/api/v1') {
  let config: AuthConfig | null = null
  let adapter: Keycloak | null = null
  let listener: TokenListener = () => undefined
  let refreshTimer: ReturnType<typeof setInterval> | undefined
  let pendingRefresh: Promise<string> | null = null
  let initPromise: Promise<AuthConfig> | null = null
  const returnKey = `jack-sso-return-${application}`
  function publish() { listener(adapter?.token || '') }
  async function init(): Promise<AuthConfig> {
    if (initPromise) return initPromise
    initPromise = (async () => {
      const response = await fetch(`${apiBase}/auth/config`, { cache: 'no-store' })
      if (!response.ok) throw new Error('无法获取统一登录配置，请检查服务连接后重试。')
      const value: AuthConfig = await response.json()
      if (!['local', 'keycloak'].includes(value.provider)) throw new Error('认证配置无效，请联系管理员。')
      config = value
      if (value.provider === 'local') return value
      const clientId = application === 'portal' ? value.portal_client_id : value.web_client_id
      if (!value.url || !value.realm || !clientId) throw new Error('统一登录配置不完整，请联系管理员。')
      if (!window.isSecureContext) throw new Error('统一登录需要 HTTPS；本机开发请使用 localhost。')
      adapter = new Keycloak({ url: value.url, realm: value.realm, clientId })
      adapter.onAuthLogout = publish
      adapter.onAuthRefreshSuccess = publish
      adapter.onAuthRefreshError = () => { adapter?.clearToken(); publish() }
      await adapter.init({
        onLoad: window.self === window.top ? 'check-sso' : undefined,
        checkLoginIframe: false,
        pkceMethod: 'S256', responseMode: 'query', flow: 'standard',
        redirectUri: `${location.origin}${location.pathname}`,
      })
      publish()
      refreshTimer = setInterval(() => { if (adapter?.authenticated) void getToken().catch(() => undefined) }, 15000)
      const savedPath = sessionStorage.getItem(returnKey)
      sessionStorage.removeItem(returnKey)
      if (adapter.authenticated && savedPath) {
        const target = new URL(savedPath, location.origin)
        if (target.origin === location.origin) history.replaceState(null, '', target.href)
      }
      return value
    })().catch((error: unknown) => { initPromise = null; throw error })
    return initPromise
  }
  async function getToken(): Promise<string> {
    if (!adapter?.authenticated) return ''
    if (pendingRefresh) return pendingRefresh
    pendingRefresh = (async () => {
      try { await adapter!.updateToken(30); publish(); return adapter!.token || '' }
      catch { adapter?.clearToken(); publish(); throw new Error('统一登录已过期，请重新登录。') }
      finally { pendingRefresh = null }
    })()
    return pendingRefresh
  }
  async function login(returnTo = `${location.pathname}${location.search}${location.hash}`) {
    await init()
    if (!adapter) throw new Error('当前未启用统一登录。')
    const target = new URL(returnTo, location.origin)
    if (target.origin !== location.origin) throw new Error('登录返回地址无效。')
    // Authentication pages deliberately cannot be framed. The opened app joins the same SSO session.
    if (window.self !== window.top) { window.open(target.href, '_blank', 'noopener,noreferrer'); return }
    sessionStorage.setItem(returnKey, target.pathname + target.search + target.hash)
    await adapter.login({ redirectUri: `${location.origin}${location.pathname}`, locale: 'zh-CN' })
  }
  async function logout() {
    if (refreshTimer) clearInterval(refreshTimer)
    const current = adapter
    if (!current) return
    const url = current.createLogoutUrl({ redirectUri: `${location.origin}/` })
    current.clearToken()
    publish()
    if (window.self !== window.top) window.open(url, '_blank', 'noopener,noreferrer')
    else location.assign(url)
  }
  function clear() { adapter?.clearToken(); publish() }
  function subscribe(onToken: TokenListener) { listener = onToken; publish() }
  return { init, getToken, login, logout, subscribe, clear, get config() { return config }, get isSso() { return config?.provider === 'keycloak' } }
}
