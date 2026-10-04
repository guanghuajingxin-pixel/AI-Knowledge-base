import request from './request'
import type { SearchResult } from '@/types/search'
import { useUserStore } from '@/stores/user'
import { sso } from '@/auth/session'

export interface ChatRequest {
  query: string
  kb_ids?: string[]
  /** 首选：知识库（检索抽象层）ID，后端按 platform 解析为 dify/ragflow 两组 dataset */
  library_ids?: number[] | null
  /** 兼容旧字段：知识源注册表 ID（推送路径登记） */
  knowledge_source_ids?: number[] | null
  /** 兼容旧字段：直接指定 Dify 数据集 ID */
  dify_dataset_ids?: string[] | null
  top_k?: number
  last_query?: string
  last_answer?: string
  /** 多轮记忆：最近若干轮 {role, content} */
  history?: { role: 'user' | 'assistant'; content: string }[]
  model?: string
  /** 模型所属供应商配置 ID（多供应商模型管理） */
  llm_profile_id?: string
  /** 会话 ID：映射 DeerFlow thread（重新对话时刷新） */
  session_id?: string
  /** 限时探索续跑动作：''=新问题；'continue'=继续探索；'stop'=先基于已检索内容回答 */
  action?: '' | 'continue' | 'stop'
}

/** Token 消耗（来自模型接口 usage，按轮次统计） */
export interface TokenUsage {
  input_tokens: number
  output_tokens: number
  total_tokens: number
}

export interface ChatResponse {
  answer_status?: 'answered' | 'partial' | 'insufficient' | 'clarification' | 'greeting'
  engine?: string
  quality?: { verification: string; dws: string; warnings: string[] }
  answer: string
  citations: SearchResult[]
  /** 全量召回快照（含未被答案引用的分段，cited 标记是否被引用）：问答明细记录用 */
  retrieval_all?: (SearchResult & { cited?: boolean; source?: string; content?: string })[]
  /** 下一步问题建议（智能体配置开启时返回） */
  follow_ups?: string[]
  classification?: string
  sufficiency?: { sufficient: boolean; missing: string }
  plan?: string
  rewritten_query?: string
  last_query?: string
  last_answer?: string
  /** 配置缺失/Agent 异常标记：llm_not_configured | agent_failed，前端据此渲染配置引导气泡 */
  config_error?: string
  /** 本轮 Token 消耗（模型接口 usage 统计） */
  usage?: TokenUsage
}

/** Agent 流式事件 */
export interface StreamEvent {
  type: 'step' | 'final' | 'config_error' | 'answer_delta' | 'choice' | 'choice_pause' | 'citations'
  step_id?: string
  status?: 'running' | 'completed' | 'failed' | 'cancelled' | 'paused'
  node?: string
  title?: string
  detail?: string
  data?: Record<string, any>
  result?: ChatResponse
  code?: string
  message?: string
  /** answer_delta 事件的回答文本增量；reset=true 时清空已累积文本（工具过渡轮回滚） */
  delta?: string
  reset?: boolean
  /** citations 事件：检索命中实时汇总的引用来源（流式填充引用面板） */
  citations?: SearchResult[]
  /** choice / choice_pause 事件：限时探索确认问题与选项按钮 */
  question?: string
  options?: string[]
  choice_kind?: string
  evidence_summary?: string
  confidence?: number
  confidence_reason?: string
  /** choice_pause / final 事件：本轮 token 消耗 */
  usage?: TokenUsage
}

export const chat = (data: ChatRequest) =>
  request.post<unknown, ChatResponse>('/search/chat', data)

/** 中断进行中的流式回答（用户点停止按钮）：通知后端停止 agent，fire-and-forget */
export const cancelChat = (sessionId: string) =>
  request
    .post('/search/chat/cancel', { session_id: sessionId })
    .catch(() => {
      /* 中断通知失败不影响前端收尾 */
    })

/**
 * 流式智能问答（SSE）。
 * onStep 收到每个节点进度；onFinal 收到最终答案；onError 收到配置/异常。
 * 返回一个 abort 函数，可中断请求。
 */
export function streamChat(
  data: ChatRequest,
  handlers: {
    onStep?: (e: StreamEvent) => void
    onDelta?: (delta: string, reset?: boolean) => void
    /** 引用来源实时汇总（检索命中即下发，流式填充引用面板） */
    onCitations?: (citations: SearchResult[]) => void
    onFinal?: (r: ChatResponse) => void
    onError?: (code: string, message: string) => void
    /** 限时探索确认：智能体请求用户选择是否继续（按钮可先渲染） */
    onChoice?: (question: string, options: string[]) => void
    /** 确认暂停落定：流结束，等待用户点选，附带本轮 token 消耗 */
    onChoicePause?: (question: string, options: string[], usage?: TokenUsage, assessment?: StreamEvent) => void
    /** 流收尾兜底：连接中断/异常结束且未收到任何终止事件（final/choice_pause/config_error） */
    onClosed?: () => void
  },
) {
  const controller = new AbortController()
  const userStore = useUserStore()
  const baseUrl = (import.meta as any).env?.VITE_API_BASE_URL || '/api/v1'
  // 是否已收到终止事件：据此判定流是否「异常收尾」，避免调用方运行态永久挂起
  let sawTerminal = false

  ;(async () => {
    try {
      const token = sso.isSso ? await sso.getToken() : userStore.token
      const resp = await fetch(`${baseUrl}/search/chat/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify(data),
        signal: controller.signal,
      })
      if (!resp.ok || !resp.body) {
        handlers.onError?.('http_error', `HTTP ${resp.status}`)
        return
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        // SSE 事件以空行分隔（兼容 \n\n 与 \r\n\r\n）
        const events = buf.split(/\r?\n\r?\n/)
        buf = events.pop() || '' // 最后一段可能不完整，保留
        for (const raw of events) {
          const line = raw.replace(/^data:\s*/, '').trim()
          if (!line) continue
          try {
              const evt: StreamEvent = JSON.parse(line)
              if (evt.type === 'step') handlers.onStep?.(evt)
              else if (evt.type === 'answer_delta') handlers.onDelta?.(evt.delta || '', evt.reset)
              else if (evt.type === 'citations') handlers.onCitations?.(evt.citations || [])
              else if (evt.type === 'final') {
                sawTerminal = true
                handlers.onFinal?.(evt.result as ChatResponse)
              } else if (evt.type === 'choice') handlers.onChoice?.(evt.question || '', evt.options || [])
              else if (evt.type === 'choice_pause') {
                sawTerminal = true
                handlers.onChoicePause?.(evt.question || '', evt.options || [], evt.usage, evt)
              } else if (evt.type === 'config_error') {
                sawTerminal = true
                handlers.onError?.(evt.code || 'unknown', evt.message || '')
              }
            } catch {
              // 忽略解析错误
            }
        }
      }
      // 流读完但未收到终止事件：服务端异常收尾（网络抖动/代理中断）
      if (!sawTerminal) handlers.onClosed?.()
    } catch (e: any) {
      if (e?.name !== 'AbortError') handlers.onError?.('network_error', e?.message || '网络错误')
      else if (!sawTerminal) handlers.onClosed?.()
    }
  })()

  return () => controller.abort()
}
