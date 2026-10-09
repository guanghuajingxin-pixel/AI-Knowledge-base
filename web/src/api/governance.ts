import request from './request'

/** 治理标准（本地可管理 + 版本 + 审核流转） */
export interface StandardDoc {
  id: string
  doc_type: string
  code: string
  version: string
  /** 最新版本状态：draft | reviewing | published | rejected */
  status: string
  effective_date: string
  link: string
  maintainer: string
  published_version_id: string | null
  latest_version_id: string | null
  created_at: string
  updated_at: string
}

/** 治理标准历史版本 */
export interface StandardVersion {
  id: string
  standard_id: string
  version_no: number
  doc_type: string
  code: string
  version: string
  status: string
  effective_date: string
  link: string
  maintainer: string
  created_by: string
  review_comment: string
  reviewed_by: string
  reviewed_at: string
  created_at: string
}

export interface StandardsResponse {
  items: StandardDoc[]
  total: number
  error?: string | null
}

/** 治理标准列表（本地数据库） */
export const getStandards = () =>
  request.get<unknown, StandardsResponse>('/governance/standards')

export interface StandardPayload {
  doc_type: string
  code: string
  version: string
  effective_date: string
  link: string
  maintainer: string
}

/** 新建标准 */
export const createStandard = (data: StandardPayload) =>
  request.post<unknown, StandardDoc>('/governance/standards', data)

/** 编辑标准（创建新版本） */
export const updateStandard = (id: string, data: StandardPayload) =>
  request.put<unknown, StandardDoc>(`/governance/standards/${id}`, data)

/** 删除标准 */
export const deleteStandard = (id: string) =>
  request.delete(`/governance/standards/${id}`)

/** 历史版本列表 */
export const getVersions = (id: string) =>
  request.get<unknown, { items: StandardVersion[]; total: number }>(
    `/governance/standards/${id}/versions`)

/** 回滚到指定版本 */
export const rollbackVersion = (stdId: string, verId: string) =>
  request.post<unknown, StandardDoc>(`/governance/standards/${stdId}/versions/${verId}/rollback`)

/** 提交审核（发钉钉动作卡片给审批人） */
export const submitReview = (id: string, reviewerUserid: string, reviewerName = '') =>
  request.post<unknown, { ok: boolean; message_error: string | null }>(
    `/governance/standards/${id}/submit-review`,
    { reviewer_userid: reviewerUserid, reviewer_name: reviewerName })

/** 审核通过 */
export const approveStandard = (id: string, comment = '') =>
  request.post<unknown, StandardDoc>(`/governance/standards/${id}/approve`, { comment })

/** 审核驳回 */
export const rejectStandard = (id: string, comment = '') =>
  request.post<unknown, StandardDoc>(`/governance/standards/${id}/reject`, { comment })

/** 从钉钉多维表导入（初始化） */
export const importStandardsFromDingtalk = () =>
  request.post<unknown, { ok: boolean; imported: number }>('/governance/standards/import-dingtalk')

/** 搜索钉钉通讯录用户（选择审批人） */
export interface DingtalkUser { userid: string; name: string }
export const searchDingtalkUsers = (q: string) =>
  request.get<unknown, { items: DingtalkUser[] }>('/governance/dingtalk/users/search', { params: { q } })

export interface KnowledgeGap {
  directory_id: string
  kb_id: string
  kb_name: string
  directory_path: string
  owner: string
  document_count: number
  /** 直接子文件夹数（不含目录本身与孙级）；钉钉行为快照内直属子文件夹，本地行为直接子目录 */
  folder_count: number
  /** 行来源：local=本地知识库目录（可维护Owner） dingtalk=钉钉知识库文件夹（实时拉取） */
  source?: 'local' | 'dingtalk'
  /** 钉钉知识库首页链接（仅 dingtalk 行） */
  kb_url?: string
  /** 该目录在钉钉中的节点链接（仅 dingtalk 行）；根目录同样可跳转知识库首页 */
  dingtalk_url?: string
}
/** 数量区间筛选预设值：''=全部，后端映射为闭区间（100+ 无上限） */
export type GapCountRange = '' | '0' | '1-9' | '10-99' | '100+'
export interface GapFilters {
  kb_id?: string
  owner?: string
  /** 目录路径模糊搜索（忽略大小写包含匹配） */
  path?: string
  document_state: 'all' | 'empty' | 'has'
  document_count?: GapCountRange
  folder_count?: GapCountRange
}
export interface GapResponse {
  items: KnowledgeGap[]
  total: number
  knowledge_bases: { id: string; name: string }[]
  owners: string[]
  error?: string | null
}
export const getGaps = (params: GapFilters & { page: number; size: number }) =>
  request.get<unknown, GapResponse>('/governance/gaps', { params })
export const exportGaps = (params: GapFilters) =>
  request.get<unknown, Blob>('/governance/gaps/export', { params, responseType: 'blob' })
/** 钉钉知识库文件夹快照刷新状态 */
export interface DingtalkRefreshStatus {
  running: boolean
  done: number
  error?: string | null
  /** 遍历触及安全上限被截断，快照可能不完整（父目录文件夹数量会虚记为 0） */
  truncated?: boolean
  folder_count: number
  fetched_at?: string | null
}
/** 触发后台刷新（全量遍历钉钉知识库文件夹，进度轮询 status 接口） */
export const refreshDingtalkFolders = (kbId: string) =>
  request.post<unknown, { ok: boolean; running: boolean }>('/governance/gaps/dingtalk/refresh', null, { params: { kb_id: kbId } })
export const getDingtalkRefreshStatus = (kbId: string) =>
  request.get<unknown, DingtalkRefreshStatus>('/governance/gaps/dingtalk/refresh/status', { params: { kb_id: kbId } })
export const getOwnerTemplate = (params?: { kb_id?: string }) =>
  request.get<unknown, Blob>('/governance/gaps/owner-template', { params, responseType: 'blob' })
export const saveGapOwner = (id: string, owner: string) =>
  request.put(`/governance/gaps/${id}/owner`, { owner })
/** 维护钉钉知识库文件夹的知识Owner（owner 留空清除） */
export const saveDingtalkOwner = (kbId: string, nodeId: string, owner: string) =>
  request.put<unknown, { ok: boolean }>('/governance/gaps/dingtalk/owner',
    { kb_id: kbId, node_id: nodeId, owner })
/** 通过钉钉机器人给知识Owner发单聊通知（该目录下的知识为空） */
export const notifyGapOwner = (kbId: string, nodeId: string) =>
  request.post<unknown, { ok: boolean; message?: string }>('/governance/gaps/dingtalk/notify',
    { kb_id: kbId, node_id: nodeId })
export const importGapOwners = (file: File) => {
  const data = new FormData()
  data.append('file', file)
  return request.post<unknown, { updated: number }>('/governance/gaps/owners/import', data)
}
