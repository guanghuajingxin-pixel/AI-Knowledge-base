import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Chat from '../index.vue'
import { streamChat } from '@/api/chat'
import { saveChatTurn } from '@/api/chat-sessions'

vi.mock('vue-router', () => ({ useRouter: () => ({ resolve: () => ({ href: '/qa' }) }), useRoute: () => ({ meta: {} }) }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ token: 'test', userInfo: { username: 'test' } }) }))
vi.mock('@/api/chat', () => ({ streamChat: vi.fn(), cancelChat: vi.fn() }))
vi.mock('@/api/knowledge-center', () => ({ listKnowledgeSources: vi.fn().mockResolvedValue([]) }))
vi.mock('@/api/settings', () => ({ listEnabledLlmModels: vi.fn().mockResolvedValue({ models: [] }) }))
vi.mock('@/api/agent', () => ({ getGreeting: vi.fn().mockResolvedValue({ models: ['test-model'], long_memory_enabled: true }) }))
vi.mock('@/api/auth', () => ({ getDingtalkConfig: vi.fn(), dingtalkLogin: vi.fn() }))
vi.mock('@/api/qa', () => ({ submitFeedback: vi.fn(), ERROR_TYPES: [] }))
vi.mock('@/api/chat-sessions', () => ({
  listChatSessions: vi.fn().mockImplementation(async () => []),
  createChatSession: vi.fn().mockResolvedValue({ id: 'session', title: '测试' }),
  saveChatTurn: vi.fn().mockResolvedValue({ assistant_message_id: 'a' }),
  getSessionMessages: vi.fn(), renameChatSession: vi.fn(), deleteChatSession: vi.fn(),
}))
let wrapper: ReturnType<typeof mount> | undefined
beforeEach(() => { vi.clearAllMocks(); vi.useFakeTimers() })
afterEach(() => { wrapper?.unmount(); vi.useRealTimers() })

async function pause() {
  vi.mocked(streamChat).mockImplementation((_req, handlers) => {
    queueMicrotask(() => handlers.onChoicePause?.('知识库证据不足，是否继续？',
      ['继续从钉钉知识库探索', '基于知识库内容回答'], undefined, {
        type: 'choice_pause', choice_kind: 'dingtalk_opt_in', evidence_summary: '制度要求审批，但未明确审批人。',
        confidence: 30, confidence_reason: '缺少负责人依据', citations: [],
      }))
    return vi.fn()
  })
  wrapper = mount(Chat, { global: { stubs: { RouterLink: true } } })
  await flushPromises()
  await wrapper.find('textarea').setValue('审批人是谁')
  await wrapper.find('.send-btn').trigger('click')
  await flushPromises()
  return wrapper
}

it('先显示摘要和置信度，等待超过旧倒计时仍不擅自探索钉钉', async () => {
  const view = await pause()
  expect(view.text()).toContain('制度要求审批，但未明确审批人。')
  expect(view.text()).toContain('30/100')
  expect(view.text()).toContain('缺少负责人依据')
  expect(view.text()).not.toContain('秒内未选择将自动继续')
  await vi.advanceTimersByTimeAsync(20000)
  expect(streamChat).toHaveBeenCalledTimes(1)
  const saved = vi.mocked(saveChatTurn).mock.calls[0][1]
  expect(saved.detail?.assessment).toEqual({ confidence: 30, reason: '缺少负责人依据' })
  expect(saved.detail?.choice).toMatchObject({ answered: false })
})

it.each([['继续从钉钉知识库探索', '继续探索', 'continue'], ['基于知识库内容回答', '先回答', 'stop']])('点击「%s」按钮（显示为「%s」）才以正确动作续跑同一会话', async (_opt, label, action) => {
  const view = await pause()
  vi.mocked(streamChat).mockImplementation(() => vi.fn())
  const button = view.findAll('.choice-btn').find(b => b.text() === label)!
  await button.trigger('click')
  await flushPromises()
  expect(streamChat).toHaveBeenCalledTimes(2)
  expect(vi.mocked(streamChat).mock.calls[1][0]).toMatchObject({ action, session_id: 'session' })
})
