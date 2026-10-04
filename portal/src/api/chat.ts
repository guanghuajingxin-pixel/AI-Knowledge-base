export interface Message { role: 'user' | 'assistant'; content: string }
export interface Citation { document_title?: string; content?: string }
export interface ChatResult { answer: string; citations?: Citation[]; config_error?: string }
export interface Identity { access_token: string; user: { username: string; role?: string; must_change_password?: boolean }; must_set_password?: boolean }
export class ApiError extends Error {
  constructor(message: string, public status = 0) { super(message); this.name = 'ApiError' }
}
async function post<T>(path: string, body: unknown, token?: string, signal?: AbortSignal): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api/v1${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify(body), signal,
    })
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw error
    throw new ApiError('暂时无法连接问答服务，请检查网络后重试。')
  }
  if (!response.ok) {
    const data: unknown = await response.json().catch(() => null)
    const detail = data && typeof data === 'object' && 'detail' in data && typeof data.detail === 'string' ? data.detail : ''
    throw new ApiError(response.status === 401 ? '账号或登录状态无效，请重新登录。' : detail || `问答服务暂不可用（${response.status}），请稍后重试。`, response.status)
  }
  return response.json() as Promise<T>
}
export async function login(username: string, password: string): Promise<Identity> {
  const result = await post<Identity>('/auth/login', { username, password })
  if (!result?.access_token || !result.user?.username) throw new ApiError('登录服务返回异常，请联系管理员。')
  if (result.must_set_password || result.user.must_change_password) throw new ApiError('请先在知识治理专家中完成密码设置，再返回门户登录。')
  return result
}
export async function ask(query: string, history: Message[], sessionId: string, token: string, signal: AbortSignal): Promise<ChatResult> {
  const result = await post<ChatResult>('/search/chat', { query, history: history.slice(-12), session_id: sessionId }, token, signal)
  if (!result || typeof result.answer !== 'string' || !result.answer.trim()) throw new ApiError('问答服务未返回答案，请重试。')
  if (result.config_error) throw new ApiError(result.answer)
  return result
}
