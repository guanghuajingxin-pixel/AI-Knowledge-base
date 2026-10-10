import request from './request'

// 知识库（检索抽象层）：统一登记 RAGFlow / DIFY 知识库镜像，供智能体检索选库。
// 仅登记 platform + dataset_id 引用，不支持导入/解析新文档；
// 与「知识源管理」（推送路径定义）相互独立。

export type LibraryPlatform = 'dify' | 'ragflow'

export interface KnowledgeLibrary {
  id: number
  name: string
  platform: LibraryPlatform
  dataset_id: string
  library_type: 'document' | 'external'
  description: string
  enabled: boolean
  updated_at: string
}

export interface KnowledgeLibraryCreate {
  name: string
  platform: LibraryPlatform
  dataset_id: string
  description?: string
  enabled?: boolean
}

export interface KnowledgeLibraryUpdate {
  name?: string
  description?: string
  enabled?: boolean
}

export const listKnowledgeLibraries = (enabledOnly = false) =>
  request.get<unknown, KnowledgeLibrary[]>('/knowledge-libraries', {
    params: enabledOnly ? { enabled_only: true } : {},
  })

export const createKnowledgeLibrary = (data: KnowledgeLibraryCreate) =>
  request.post<unknown, KnowledgeLibrary>('/knowledge-libraries', data)

export const updateKnowledgeLibrary = (id: number, data: KnowledgeLibraryUpdate) =>
  request.put<unknown, KnowledgeLibrary>(`/knowledge-libraries/${id}`, data)

export const deleteKnowledgeLibrary = (id: number) =>
  request.delete<unknown, { ok: boolean }>(`/knowledge-libraries/${id}`)

// ===== 检索测试：按知识库逐库执行与智能问答一致的底层检索 =====

export interface RetrievalTestHit {
  semantic_weight?: number
  score_type?: string
  token_similarity?: number
  vector_similarity?: number
  rerank_score?: number
  matched_content?: string
  retrieval_enhancements?: {
    filename?: string
    summary?: string
    questions?: string[]
    image_captions?: Array<{image: string; caption: string}>
    errors?: Record<string, string>
  }
  score: number
  content: string
  document_title: string
  document_id?: string
  dataset_id?: string
  segment_id?: string
  page_number?: number | null
  source?: string
  library_id: number
  library_name: string
}

export interface RetrievalTestLib {
  library_id: number
  name: string
  platform: LibraryPlatform
  dataset_id: string
  enabled: boolean
  ok: boolean
  count: number
  error: string
}

export interface RetrievalTestResult {
  query: string
  top_k: number
  elapsed_ms: number
  libraries: RetrievalTestLib[]
  hits: RetrievalTestHit[]
  total: number
}

export interface RetrievalTestParams {
  document_ids?: string[]
  rerank?: boolean
  rerank_model_id?: string
  query: string
  library_ids?: number[]
  top_k?: number
  /** 检索模式：hybrid=混合检索（默认）、vector=向量检索、fulltext=全文检索 */
  mode?: 'hybrid' | 'vector' | 'fulltext'
  /** 混合检索 + Rerank 子策略时的 RAGFlow rerank 模型名 */
  rerank_id?: string
  /** 最终相关性分数阈值 */
  similarity_threshold?: number
  /** 本地与 RagFlow 混合检索的向量权重 */
  vector_similarity_weight?: number
}

export const testKnowledgeRetrieval = (data: RetrievalTestParams) =>
  request.post<unknown, RetrievalTestResult>('/knowledge-libraries/retrieval-test', data)
