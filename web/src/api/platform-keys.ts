import request from './request'

export interface PlatformKey {
  id: string
  client_id: string
  name: string
  enabled: boolean
  created_at: string | null
  permissions: string[]
}
export interface PlatformKeyCreated extends PlatformKey { raw_key: string }
// request.ts 已配置 /api/v1，模块只传资源路径，避免重复前缀。
const base = '/platform/api-keys'
export const listPlatformKeys = () => request.get<never, PlatformKey[]>(base)
export const createPlatformKey = (name: string) => request.post<never, PlatformKeyCreated>(base, { name })
export const updatePlatformKey = (id: string, data: { name?: string; enabled?: boolean }) =>
  request.patch<never, PlatformKey>(`${base}/${id}`, data)
export const deletePlatformKey = (id: string) => request.delete(`${base}/${id}`)

/** 回显平台 API Key 完整密钥（client_id:secret，仅管理员） */
export const revealPlatformKeySecret = (id: string) =>
  request.get<never, { client_id: string; raw_key: string }>(`${base}/${id}/secret`)
