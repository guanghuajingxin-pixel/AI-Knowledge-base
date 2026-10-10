import request from './request'

export interface StorageMetric {
  value: string | null
  unit: string
  month_new: string
  mom: string
  error?: string | null
  fallback?: boolean
  loading?: boolean
  updated_at?: string | null
}

export interface KnowledgeCountMetric {
  value: number | null
  unit: string
  month_new: number | null
  mom: string
  loading?: boolean
  error?: string | null
  /** 遍历时有目录因钉钉限流未取到，计数可能偏小 */
  partial?: boolean
  failed_folders?: number
  updated_at?: string | null
}

export interface OverviewResponse {
  storage: StorageMetric
  knowledge_count: KnowledgeCountMetric
  dingtalk_configured?: boolean
  job?: { running: boolean; phase: string | null }
}

export interface DistributionItem {
  name: string
  value: number
  workspace_id?: string
}

export const getOverview = () =>
  request.get<unknown, OverviewResponse>('/operate/overview')

export const getKnowledgeDistribution = (topN?: number) =>
  request.get<unknown, { items: DistributionItem[]; error?: string; loading?: boolean }>(
    '/operate/knowledge-distribution',
    { params: topN ? { top_n: topN } : {} }
  )

export const refreshDingtalk = () =>
  request.post<unknown, { ok: boolean; error?: string; storage_error?: string | null; loading?: boolean; already_running?: boolean }>(
    '/operate/refresh-dingtalk'
  )

export interface HotDocItem {
  rank: number
  node_id: string
  title: string
  workspace: string
  url: string
  read_count: number
  /** 访问（查看）次数，排序主指标 */
  count: number
}

export interface HotDocsResponse {
  items: HotDocItem[]
  scanned: number
  total: number
  loading: boolean
  error?: string | null
  updated_at?: string | null
}

export const getHotDocuments = () => request.get<unknown, HotDocsResponse>('/operate/hot-documents')

export interface KnowledgeGapDepartment {
  department: string
  expected_folder_count: number
  missing_folder_count: number
  uploaded_file_count: number
  covered_folder_count: number
  coverage_percent: number
}

export interface KnowledgeGapDepartmentsResponse {
  items: KnowledgeGapDepartment[]
  total: KnowledgeGapDepartment
  updated_at: string | null
  has_snapshot: boolean
}

export const getKnowledgeGapDepartments = () =>
  request.get<unknown, KnowledgeGapDepartmentsResponse>('/operate/knowledge-gap-departments')
