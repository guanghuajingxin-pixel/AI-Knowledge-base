/**
 * 知识中心类型定义
 */

/** 入库审核状态（空串 = 未审核） */
export type DingTalkReviewStatus = '通过' | '待确认' | '待更正'

/** 钉钉知识库文件（实时拉取自钉钉开放平台） */
export interface DingTalkFile {
  node_id: string
  workspace_id: string
  workspace_name: string
  name: string
  /** 上级目录路径，多层目录用 / 分隔，根目录为 "/" */
  directory_path: string
  category: string | null
  extension: string | null
  size: number
  url: string | null
  creator_id: string
  creator_name: string | null
  created_at: string | null
  modified_at: string | null
  /** 入库审核状态：通过/待确认/待更正，空串为未审核 */
  review_status?: DingTalkReviewStatus | ''
  /** 执行审核动作的用户名 */
  reviewer?: string
  reviewed_at?: string | null
}

/** 钉钉知识库 / 创建人选项 */
export interface DingTalkOption {
  id: string
  name: string
}

/** 钉钉团队知识库（实时列表，含根节点 ID） */
export interface DingTalkWorkspace {
  id: string
  name: string
  root_node_id: string
}

/** 钉钉目录子节点（实时，按父节点查询） */
export interface DingTalkNode {
  node_id: string
  name: string
  is_folder: boolean
  has_children: boolean
  extension: string | null
  size: number
  url: string | null
  creator_id: string
  created_at: string | null
  modified_at: string | null
}

/** 钉钉文件列表查询结果 */
export interface DingTalkDocResult {
  items: DingTalkFile[]
  total: number
  page: number
  size: number
  workspaces: DingTalkOption[]
  creators: DingTalkOption[]
  /** 后台正在遍历钉钉知识库时为 true（前端轮询） */
  loading?: boolean
  error?: string | null
  /** 最近一次成功刷新（持久化快照）的时间（UTC ISO），用于展示数据新鲜度 */
  cached_at?: string | null
}

/** 知识源登记（企业知识库注册表） */
export interface KnowledgeSource {
  id: number
  name: string
  source_type: 'dingtalk_workspace' | 'dify_dataset' | 'ragflow_dataset' | 'business_system'
  external_id: string
  description: string
  config: Record<string, any> | null
  enabled: boolean
  created_at: string
  updated_at: string
}

/** 知识源创建/更新参数 */
export interface KnowledgeSourcePayload {
  name: string
  source_type: KnowledgeSource['source_type']
  external_id: string
  description?: string
  config?: Record<string, any> | null
  enabled?: boolean
}

/** 知识中心文档（跨文档库聚合的知识源文档，原件存于 MinIO） */
export interface SourceDoc {
  id: string
  name: string
  tags: string[]
  /** local=手动上传 | dingtalk=钉钉知识库 */
  source: 'local' | 'dingtalk'
  status: string
  size: number
  library_id: number
  library_name: string
  /** 更新人：手动上传=操作用户，钉钉同步=同步触发人 */
  updated_by: string | null
  /** 过期时间（UTC ISO，null 未设置） */
  expire_at: string | null
  /** 更新时间（UTC ISO） */
  updated_at: string | null
  /** 钉钉在线文档链接（仅 source=dingtalk，标题超链接跳转用） */
  source_url: string | null
  /** 钉钉知识库名称（仅 source=dingtalk，来源系统展示用） */
  source_workspace_name: string | null
}

/** 知识中心文档列表查询结果 */
export interface SourceDocResult {
  items: SourceDoc[]
  total: number
}
