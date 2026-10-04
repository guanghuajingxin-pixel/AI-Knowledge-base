import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { ApiError, ask, type Citation, type Message } from '../api/chat'
import { identity, accessToken, signIn as authenticate, signOut as logout } from '../auth/session'

interface Turn extends Message { citations?: Citation[] }
interface Conversation { id: string; title: string; messages: Turn[] }
let sequence = 0
function newId(): string { return `portal-${Date.now()}-${++sequence}-${Math.random().toString(36).slice(2)}` }

export function useChat() {
  const conversations = ref<Conversation[]>([])
  const activeId = ref<string>(newId())
  const draft = ref<string>('')
  const isSending = ref<boolean>(false)
  const isLoginOpen = ref<boolean>(false)
  const isLoggingIn = ref<boolean>(false)
  const loginError = ref<string>('')
  const error = ref<string>('')
  const username = ref<string>('')
  const password = ref<string>('')
  const messageArea = ref<HTMLElement | null>(null)
  let controller: AbortController | null = null
  let sendAfterLogin = false
  const current = computed(() => conversations.value.find(item => item.id === activeId.value))
  const messages = computed(() => current.value?.messages || [])
  async function scrollToEnd() {
    await nextTick()
    messageArea.value?.scrollTo({ top: messageArea.value.scrollHeight, behavior: 'smooth' })
  }
  function stop() { controller?.abort() }
  function newConversation() { stop(); activeId.value = newId(); draft.value = ''; error.value = '' }
  function selectConversation(id: string) { stop(); activeId.value = id; draft.value = ''; error.value = ''; void scrollToEnd() }
  async function send() {
    const query = draft.value.trim()
    if (!query || isSending.value) return
    if (!identity.value) { sendAfterLogin = true; isLoginOpen.value = true; return }
    error.value = ''
    let conversation = current.value
    if (!conversation) {
      conversations.value.unshift({ id: activeId.value, title: query.slice(0, 24), messages: [] })
      conversation = current.value!
    }
    const history = conversation.messages.map(({ role, content }) => ({ role, content }))
    conversation.messages.push({ role: 'user', content: query })
    draft.value = ''
    isSending.value = true
    const requestController = new AbortController()
    controller = requestController
    void scrollToEnd()
    try {
      const result = await ask(query, history, conversation.id, await accessToken(), requestController.signal)
      if (requestController.signal.aborted) return
      conversation.messages.push({ role: 'assistant', content: result.answer, citations: result.citations })
    } catch (cause) {
      // A failed/cancelled request must not become a successful turn in the model history.
      conversation.messages.pop()
      if (requestController === controller && conversation.id === activeId.value) {
        draft.value = query
        if (!(cause instanceof Error && cause.name === 'AbortError')) {
          error.value = cause instanceof Error ? cause.message : '问答失败，请重试。'
          if (cause instanceof ApiError && cause.status === 401) { identity.value = null; isLoginOpen.value = true }
        }
      }
    } finally {
      if (requestController === controller) { isSending.value = false; controller = null; void scrollToEnd() }
    }
  }
  async function signIn() {
    if (!username.value.trim() || !password.value || isLoggingIn.value) return
    isLoggingIn.value = true
    loginError.value = ''
    try {
      await authenticate(username.value.trim(), password.value)
      password.value = ''
      isLoginOpen.value = false
      if (sendAfterLogin) { sendAfterLogin = false; void send() }
    } catch (cause) { loginError.value = cause instanceof Error ? cause.message : '登录失败，请重试。' }
    finally { isLoggingIn.value = false }
  }
  function closeLogin() { password.value = ''; loginError.value = ''; sendAfterLogin = false }
  function signOut() { stop(); conversations.value = []; newConversation(); void logout() }
  onBeforeUnmount(stop)
  return { conversations, activeId, draft, identity, isSending, isLoginOpen, isLoggingIn, loginError, error, username, password, messageArea, messages, newConversation, selectConversation, send, signIn, signOut, closeLogin, stop }
}
