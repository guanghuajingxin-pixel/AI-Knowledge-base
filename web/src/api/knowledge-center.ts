import request from './request'
import type { DingTalkDocResult, DingTalkWorkspace, DingTalkNode, KnowledgeSource, KnowledgeSourcePayload, SourceDoc, SourceDocResult } from '@/types/knowledge-center'

/** 钉钉知识库文件列表（读取服务端持久化快照；refresh=true 才后台重新遍历钉钉） */
export function fetchDingTalkDocuments(params: {
  page: number
  size: number
  workspace_id?: string
  creator_id?: string
  search?: string
  directory?: string
  refresh?: boolean
}) {
  // 后端读持久化快照即返回（refresh=true 也是立即返回 + 后台遍历），
  // 无需 10 分钟超时；回落全局 60s，避免异常时长时间挂住一条请求占后端连接。
  return request.get<unknown, DingTalkDocResult>('/knowledge-center/dingtalk/documents', { params })
}

/** 入库审核：记录/变更钉钉文件的审核状态（审核人由后端记录为当前登录用户名） */
export function reviewDingTalkDocument(params: {
  node_id: string
  workspace_id?: string
  review_status: '通过' | '待确认' | '待更正'
}) {
  return request.put<unknown, {
    ok: boolean
    node_id: string
    review_status: string
    reviewer: string
    reviewed_at: string | null
  }>('/knowledge-center/dingtalk/documents/review', params)
}

/** 实时列出钉钉团队知识库（单次 API 调用，轻量） */
export function fetchDingTalkWorkspaces() {
  return request.get<unknown, { items: DingTalkWorkspace[]; error: string | null }>('/knowledge-center/dingtalk/workspaces', { timeout: 60000 })
}

/** 实时列出钉钉某父节点下的直接子节点（单次 API 调用，轻量） */
export function fetchDingTalkNodes(parentNodeId: string) {
  return request.get<unknown, { items: DingTalkNode[] }>('/knowledge-center/dingtalk/nodes', {
    params: { parent_node_id: parentNodeId },
    timeout: 60000,
  })
}

// ========== 知识源登记（企业知识库注册表）==========

/** 知识库列表：系统内所有「选择知识库」的地方均从此接口取数 */
export function listKnowledgeSources(params?: { source_type?: string; enabled_only?: boolean }) {
  return request.get<unknown, KnowledgeSource[]>('/knowledge-center/knowledge-sources', { params })
}

/** 新增知识库登记 */
export function createKnowledgeSource(payload: KnowledgeSourcePayload) {
  return request.post<unknown, KnowledgeSource>('/knowledge-center/knowledge-sources', payload)
}

/** 更新知识库登记 */
export function updateKnowledgeSource(id: number, payload: Partial<KnowledgeSourcePayload>) {
  return request.put<unknown, KnowledgeSource>(`/knowledge-center/knowledge-sources/${id}`, payload)
}

/** 删除知识库登记 */
export function deleteKnowledgeSource(id: number) {
  return request.delete<unknown, { ok: boolean }>(`/knowledge-center/knowledge-sources/${id}`)
}

/** 获取某知识库下的目录（通过知识库 ID 获取目录结构） */
export function fetchKnowledgeSourceDirectories(sourceId: number, parentNodeId?: string) {
  return request.get<unknown, { source_type: string; root_node_id: string; items: any[] }>(
    `/knowledge-center/knowledge-sources/${sourceId}/directories`,
    { params: parentNodeId ? { parent_node_id: parentNodeId } : {} },
  )
}

/** 钉钉知识库文件夹快照（dingtalk_folder_stats 表，只读 DB 不调钉钉，秒开） */
export function fetchDingtalkFolderSnapshot(sourceId: number) {
  return request.get<unknown, { external_id: string; fetched_at: string | null; count: number; folders: Array<{ node_id: string; path: string }> }>(
    `/knowledge-center/knowledge-sources/${sourceId}/dingtalk-folder-snapshot`,
  )
}

// ========== 知识中心 · 知识源文档（跨文档库聚合）==========

/** 知识源文档列表：所有来源的知识文档（元数据来自 DB，原件存于 MinIO） */
export function fetchSourceDocuments(params: {
  page: number
  size: number
  search?: string
  source?: string
  tag?: string
}) {
  return request.get<unknown, SourceDocResult>('/knowledge-center/source-documents', { params })
}

/** 知识中心标签筛选选项：文档库全部标签去重计数（按使用数降序） */
export function fetchSourceDocumentTags() {
  return request.get<unknown, { items: Array<{ name: string; count: number }> }>('/knowledge-center/source-documents/tags')
}

/** 编辑知识中心文档：标签 + 过期时间 */
export function updateSourceDocument(id: string, payload: { tags: string[]; expire_at: string | null }) {
  return request.put<unknown, SourceDoc>(`/knowledge-center/source-documents/${id}`, payload)
}
