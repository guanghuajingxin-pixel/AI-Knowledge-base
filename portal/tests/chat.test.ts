import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, ask, login } from '../src/api/chat'
import { pageFromHash } from '../src/config/apps'

const token = 'unit-test-token'
afterEach(() => vi.unstubAllGlobals())
function mockResponse(body: unknown, status = 200) {
  const mock = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', mock)
  return mock
}
describe('enterprise chat contract', () => {
  it('passes the user token and limits conversation history', async () => {
    const fetch = mockResponse({ answer: '回答', citations: [] })
    await ask('问题', Array.from({ length: 20 }, (_, i) => ({ role: 'user' as const, content: String(i) })), 'session-1', token, new AbortController().signal)
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('/api/v1/search/chat')
    expect(init.headers.Authorization).toBe(`Bearer ${token}`)
    expect(JSON.parse(init.body)).toEqual({ query: '问题', session_id: 'session-1', history: Array.from({ length: 12 }, (_, i) => ({ role: 'user', content: String(i + 8) })) })
  })
  it('requires password setup without granting a portal session', async () => {
    mockResponse({ access_token: token, user: { username: 'test', must_change_password: true } })
    await expect(login('test', 'test')).rejects.toThrow('完成密码设置')
  })
  it('keeps expired authentication distinguishable from service failures', async () => {
    mockResponse({}, 401)
    await expect(ask('问题', [], 'id', token, new AbortController().signal)).rejects.toMatchObject({ status: 401 })
  })
  it('shows configuration errors rather than fabricating an assistant answer', async () => {
    mockResponse({ answer: '请配置问答模型', config_error: 'llm_not_configured' })
    await expect(ask('问题', [], 'id', token, new AbortController().signal)).rejects.toThrow('请配置问答模型')
  })
  it('rejects an empty answer', async () => {
    mockResponse(null)
    await expect(ask('问题', [], 'id', token, new AbortController().signal)).rejects.toThrow('未返回答案')
  })
  it('preserves user cancellation', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new DOMException('Aborted', 'AbortError')))
    await expect(ask('问题', [], 'id', token, new AbortController().signal)).rejects.toMatchObject({ name: 'AbortError' })
  })
  it('handles unavailable network with a recoverable error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(login('test', 'test')).rejects.toBeInstanceOf(ApiError)
  })
})
describe('portal navigation', () => {
  it('restores application deep links and rejects unknown pages', () => {
    expect(pageFromHash('#/knowledge')).toBe('knowledge')
    expect(pageFromHash('#/evaluation')).toBe('evaluation')
    expect(pageFromHash('#/skillhub')).toBe('skillhub')
    expect(pageFromHash('#/https://example.com')).toBe('home')
    expect(pageFromHash('')).toBe('home')
  })
})
