import request from './request'

export interface SettingItem {
  label: string
  value: string
  is_set: boolean
  is_secret: boolean
}

export type SettingsResponse = Record<string, SettingItem>

export const getSettings = () => request.get<unknown, SettingsResponse>('/settings')

export const setSetting = (data: { key: string; value: string }) =>
  request.put<unknown, { ok: boolean }>('/settings', data)

/** 站点外观（侧边栏 Logo 名称与图标，所有登录用户可读） */
export interface SiteBranding {
  site_name: string
  site_logo: string
}

export const getSiteBranding = () => request.get<unknown, SiteBranding>('/settings/site')

export interface TestLLMResult {
  ok: boolean
  latency_ms?: number
  model?: string
  reply?: string
  message?: string
}

export const testLLM = (data: { base_url?: string; api_key?: string; model?: string; profile_id?: string }) =>
  request.post<unknown, TestLLMResult>('/settings/test-llm', data)

export interface LlmModelsResult {
  ok: boolean
  models: string[]
  message?: string
}

export const listLlmModels = (params: { base_url?: string; api_key?: string }) =>
  request.get<unknown, LlmModelsResult>('/settings/llm-models', { params })

// ============ Dify 多配置 ============

export interface DifyProfile {
  id: string
  name: string
  base_url: string
  api_key: string
  dataset_ids: string
  enabled: boolean
  created_at: string | null
}

export const listDifyProfiles = () =>
  request.get<unknown, DifyProfile[]>('/settings/dify-profiles')

export const createDifyProfile = (data: Omit<DifyProfile, 'id' | 'enabled' | 'created_at'>) =>
  request.post<unknown, DifyProfile>('/settings/dify-profiles', data)

export const updateDifyProfile = (id: string, data: Omit<DifyProfile, 'id' | 'enabled' | 'created_at'>) =>
  request.put<unknown, DifyProfile>(`/settings/dify-profiles/${id}`, data)

export const deleteDifyProfile = (id: string) =>
  request.delete<unknown, void>(`/settings/dify-profiles/${id}`)

export const enableDifyProfile = (id: string) =>
  request.put<unknown, { ok: boolean }>(`/settings/dify-profiles/${id}/enable`)

// ============ LLM 供应商配置（多供应商 × 多模型） ============

export interface LlmModelEntry {
  name: string
  enabled: boolean
  is_default: boolean
}

export interface LlmProfile {
  id: string
  name: string
  provider: string
  base_url: string
  api_key: string          // 脱敏回显
  has_key: boolean
  models: LlmModelEntry[]
  created_at: string | null
}

export interface EnabledLlmModel {
  profile_id: string
  profile_name: string
  provider: string
  model: string
  is_default: boolean
}

export const listLlmProfiles = () =>
  request.get<unknown, LlmProfile[]>('/settings/llm-profiles')

export const createLlmProfile = (data: {
  name: string; provider: string; base_url: string; api_key: string; models: LlmModelEntry[]
}) => request.post<unknown, { ok: boolean; id: string }>('/settings/llm-profiles', data)

export const updateLlmProfile = (id: string, data: {
  name: string; provider: string; base_url: string; api_key: string; models: LlmModelEntry[]
}) => request.put<unknown, { ok: boolean }>(`/settings/llm-profiles/${id}`, data)

export const deleteLlmProfile = (id: string) =>
  request.delete<unknown, void>(`/settings/llm-profiles/${id}`)

export const refreshLlmProfileModels = (id: string) =>
  request.post<unknown, { ok: boolean; models: LlmModelEntry[] }>(`/settings/llm-profiles/${id}/refresh-models`)

export const listEnabledLlmModels = () =>
  request.get<unknown, { models: EnabledLlmModel[] }>('/settings/llm-enabled-models')

export interface TestDingtalkResult {
  ok: boolean
  latency_ms?: number
  workspace_count?: number
  message?: string
}

export const testDingtalk = (data: { app_key?: string; app_secret?: string; operator_union_id?: string }) =>
  request.post<unknown, TestDingtalkResult>('/settings/test-dingtalk', data)

export interface TestDifyResult {
  ok: boolean
  latency_ms?: number
  message?: string
}

export const testDify = (data: { base_url?: string; api_key?: string; profile_id?: string }) =>
  request.post<unknown, TestDifyResult>('/settings/test-dify', data)

export interface TestMinerUResult {
  ok: boolean
  latency_ms?: number
  message?: string
}

export const testMineru = (data: { api_key?: string }) =>
  request.post<unknown, TestMinerUResult>('/settings/test-mineru', data)

// ============ Embedding / Rerank 模型连通性测试 ============

export interface TestEmbeddingResult {
  ok: boolean
  latency_ms?: number
  dimension?: number
  message?: string
}

export const testEmbedding = (data: { base_url?: string; api_key?: string; model?: string; profile_id?: string }) =>
  request.post<unknown, TestEmbeddingResult>('/settings/test-embedding', data)

export interface TestRerankResult {
  ok: boolean
  latency_ms?: number
  message?: string
}

export const testRerank = (data: { api_url?: string; api_key?: string; model?: string; profile_id?: string }) =>
  request.post<unknown, TestRerankResult>('/settings/test-rerank', data)

// ============ Rerank 重排模型多配置 ============

export interface RerankProfile {
  id: string
  name: string
  api_url: string
  api_key: string          // 脱敏回显
  has_key: boolean
  model: string
  enabled: boolean
  created_at: string | null
}

export const listRerankProfiles = () =>
  request.get<unknown, RerankProfile[]>('/settings/rerank-profiles')

export const createRerankProfile = (data: { name: string; api_url: string; api_key: string; model: string }) =>
  request.post<unknown, RerankProfile>('/settings/rerank-profiles', data)

export const updateRerankProfile = (id: string, data: { name: string; api_url: string; api_key: string; model: string }) =>
  request.put<unknown, RerankProfile>(`/settings/rerank-profiles/${id}`, data)

export const deleteRerankProfile = (id: string) =>
  request.delete<unknown, void>(`/settings/rerank-profiles/${id}`)

export const enableRerankProfile = (id: string, enabled = true) =>
  request.put<unknown, { ok: boolean }>(`/settings/rerank-profiles/${id}/enable`, { enabled })

// ============ Embedding 向量模型多配置 ============

export interface EmbeddingProfile {
  id: string
  name: string
  api_url: string
  api_key: string          // 脱敏回显
  has_key: boolean
  model: string
  enabled: boolean
  created_at: string | null
}

export const listEmbeddingProfiles = () =>
  request.get<unknown, EmbeddingProfile[]>('/settings/embedding-profiles')

export const createEmbeddingProfile = (data: { name: string; api_url: string; api_key: string; model: string }) =>
  request.post<unknown, EmbeddingProfile>('/settings/embedding-profiles', data)

export const updateEmbeddingProfile = (id: string, data: { name: string; api_url: string; api_key: string; model: string }) =>
  request.put<unknown, EmbeddingProfile>(`/settings/embedding-profiles/${id}`, data)

export const deleteEmbeddingProfile = (id: string) =>
  request.delete<unknown, void>(`/settings/embedding-profiles/${id}`)

export const enableEmbeddingProfile = (id: string) =>
  request.put<unknown, { ok: boolean }>(`/settings/embedding-profiles/${id}/enable`)

// ============ OpenAI 兼容服务模型列表拉取（不过滤，供 Embedding/Rerank 弹窗） ============

export const fetchExternalModels = (baseUrl: string, apiKey: string, extra?: { kind?: string; profile_id?: string }) =>
  request.get<unknown, { ok: boolean; models: string[]; message?: string }>('/settings/external-models', {
    params: { base_url: baseUrl, api_key: apiKey, ...(extra || {}) },
  })

// ============ RAGFlow 知识库多配置（多环境切换） ============

export interface RagflowProfile {
  id: string
  name: string
  base_url: string
  api_key: string          // 脱敏回显
  has_key: boolean
  enabled: boolean
  created_at: string | null
}

export const listRagflowProfiles = () =>
  request.get<unknown, RagflowProfile[]>('/settings/ragflow-profiles')

export const createRagflowProfile = (data: { name: string; base_url: string; api_key: string }) =>
  request.post<unknown, RagflowProfile>('/settings/ragflow-profiles', data)

export const updateRagflowProfile = (id: string, data: { name: string; base_url: string; api_key: string }) =>
  request.put<unknown, RagflowProfile>(`/settings/ragflow-profiles/${id}`, data)

export const deleteRagflowProfile = (id: string) =>
  request.delete<unknown, void>(`/settings/ragflow-profiles/${id}`)

export const enableRagflowProfile = (id: string) =>
  request.put<unknown, { ok: boolean }>(`/settings/ragflow-profiles/${id}/enable`)

// ============ 密钥回显（管理员查看/复制完整密钥） ============

/** 查看 profile 配置的完整 API Key（kind: dify / llm / ragflow / embedding / rerank） */
export const revealProfileSecret = (kind: 'dify' | 'llm' | 'ragflow' | 'embedding' | 'rerank', profileId: string) =>
  request.get<unknown, { api_key: string }>(`/settings/profile-secret/${kind}/${profileId}`)

/** 查看系统级密钥配置项的完整值（如 mineru_api_key / dingtalk_app_secret） */
export const revealSettingSecret = (key: string) =>
  request.get<unknown, { value: string }>(`/settings/setting-secret/${key}`)

/** 复制文本到剪贴板（http 内网环境降级 execCommand） */
export async function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  try { document.execCommand('copy') } finally { document.body.removeChild(ta) }
}

// ============ 菜单显示配置（侧边栏功能区菜单显隐） ============

export const getMenuVisibility = () =>
  request.get<unknown, { hidden: string[] }>('/settings/menu-visibility')

export const setMenuVisibility = (data: { hidden: string[] }) =>
  request.put<unknown, { ok: boolean; hidden: string[] }>('/settings/menu-visibility', data)

// ============ 钉钉机器人运行状态（系统配置页展示） ============

export interface DingtalkBotStatus {
  enabled: boolean
  running: boolean
  connected: boolean
  last_error: string
  started_at: string | null
  qa_concurrency: number
}

export const getDingtalkBotStatus = () =>
  request.get<unknown, DingtalkBotStatus>('/settings/dingtalk-bot-status')
