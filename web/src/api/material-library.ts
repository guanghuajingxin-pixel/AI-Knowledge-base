import request from './request'

export interface ImageComponent {
  id: string; name: string; kind: 'embedding' | 'remover'; provider: 'dashscope' | 'http'
  endpoint: string; model: string; dimensions: number; has_api_key: boolean
}
export interface ImageComponentInput extends Omit<ImageComponent, 'id' | 'has_api_key'> { api_key: string }
export interface MaterialLibraryConfig { embedding_id: string | null; remover_id: string | null; preprocess: boolean }
export interface MaterialLibrary { id: number; name: string; description: string; enabled: boolean; library_type: 'material'; engine_config: MaterialLibraryConfig }
export interface Material { id: string; library_id: number; code: string; name: string; specification: string; category: string; tags: string[]; description: string; enabled: boolean; image_count?: number }
export type MaterialInput = Omit<Material, 'id' | 'library_id' | 'image_count'>
export interface MaterialImage {
  id: string; material_id: string | null; filename: string; status: string; message: string
  enabled: boolean; preprocess: boolean; embedding_id: string; remover_id: string | null
  run_id: string; crop: number[] | null; has_standard: boolean; has_foreground: boolean
}
export interface ImageHit { image_id: string; run_id: string; material_id: string | null; filename: string; material: Material | null; cosine_score: number; component_name: string; group_id: string; rank_score: number }
export interface ImageSearchResult { hits: ImageHit[]; previews: { component_name: string; group_id: string; preprocess: boolean; standard: string }[]; elapsed_ms: number; message: string }
export const listImageComponents = () => request.get<unknown, ImageComponent[]>('/image-components')
export const createImageComponent = (body: ImageComponentInput) => request.post<unknown, ImageComponent>('/image-components', body)
export const updateImageCredential = (id: string, api_key: string) => request.patch(`/image-components/${id}/credential`, { api_key })
export const deleteImageComponent = (id: string) => request.delete(`/image-components/${id}`)
export const testImageComponent = (id: string, file: File, crop?: number[] | null) => {
  const form = new FormData(); form.append('file', file)
  if (crop) form.append('crop', JSON.stringify(crop))
  return request.post<unknown, { ok: boolean; dimensions: number | null; elapsed_ms: number; preview: string }>(`/image-components/${id}/test`, form, { timeout: 180000 })
}
export const listMaterialLibraries = () => request.get<unknown, MaterialLibrary[]>('/material-libraries')
export const saveMaterialLibrary = (id: number | null, body: { name: string; description: string; enabled: boolean; config: MaterialLibraryConfig }) => id
  ? request.put<unknown, MaterialLibrary>(`/material-libraries/${id}`, body)
  : request.post<unknown, MaterialLibrary>('/material-libraries', body)
export const deleteMaterialLibrary = (id: number) => request.delete(`/material-libraries/${id}`)
export const listMaterials = (lib: number, params: { q?: string; page?: number; page_size?: number } = {}) => request.get<unknown, { items: Material[]; total: number }>(`/material-libraries/${lib}/materials`, { params })
export const getMaterial = (lib: number, id: string) => request.get<unknown, Material>(`/material-libraries/${lib}/materials/${id}`)
export const saveMaterial = (lib: number, id: string | null, body: MaterialInput) => id
  ? request.put<unknown, Material>(`/material-libraries/${lib}/materials/${id}`, body)
  : request.post<unknown, Material>(`/material-libraries/${lib}/materials`, body)
export const deleteMaterial = (lib: number, id: string) => request.delete(`/material-libraries/${lib}/materials/${id}`)
export const listMaterialImages = (lib: number, params: { material_id?: string; unassigned?: boolean; page?: number; page_size?: number } = {}) => request.get<unknown, { items: MaterialImage[]; total: number }>(`/material-libraries/${lib}/images`, { params })
export const uploadMaterialImage = (lib: number, file: File, materialId: string | null, preprocess: boolean, crop?: number[] | null) => {
  const form = new FormData(); form.append('file', file); form.append('preprocess', String(preprocess))
  if (materialId) form.append('material_id', materialId)
  if (crop) form.append('crop', JSON.stringify(crop))
  return request.post<unknown, MaterialImage>(`/material-libraries/${lib}/images`, form, { timeout: 180000 })
}
export const editMaterialImage = (lib: number, id: string, body: { enabled?: boolean; material_id?: string | null }) => request.patch(`/material-libraries/${lib}/images/${id}`, body)
export const retryMaterialImage = (lib: number, id: string, body: { use_library_config?: boolean; preprocess?: boolean; crop?: number[] | null }) => request.post<unknown, { status: string; message: string }>(`/material-libraries/${lib}/images/${id}/retry`, body)
export const deleteMaterialImage = (lib: number, id: string) => request.delete(`/material-libraries/${lib}/images/${id}`)
export const materialImageBlob = (lib: number, id: string, variant: 'original' | 'standard' | 'foreground') => request.get<unknown, Blob>(`/material-libraries/${lib}/images/${id}/${variant}`, { responseType: 'blob' })
export const searchMaterialImages = (lib: number, file: File, params: { top_k: number; category: string; specification: string; crop?: number[] | null }) => {
  const form = new FormData(); form.append('file', file)
  form.append('top_k', String(params.top_k)); form.append('category', params.category); form.append('specification', params.specification)
  if (params.crop) form.append('crop', JSON.stringify(params.crop))
  return request.post<unknown, ImageSearchResult>(`/material-libraries/${lib}/search`, form, { timeout: 300000 })
}
