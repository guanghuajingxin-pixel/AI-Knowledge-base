import request from './request'
export interface ProcessingConfig {
  chunk_method: string; layout_recognize: string; chunk_token_num: number; delimiter: string; embedding_model: string; enable_children: boolean; children_delimiter: string; auto_keywords: number; auto_questions: number;
  overlap: number; replace_whitespace: boolean; remove_urls_emails: boolean
}
export interface DocumentLibrary {
  id: number; name: string; description: string; enabled: boolean; document_count: number; creator?: string; config: DocumentIndexConfig; created_at: string
}
export interface LibraryDocument {
  id: string; name: string; size: number; status: string; progress: number; message: string;
  chunk_count: number; enabled: boolean; source: string; parsed_at: string | null;
  tags: string[];
  created_at: string; updated_at: string;
  config?: DocumentIndexConfig
}
export interface Enhancements { include_filename: boolean; auto_summary: boolean; auto_questions: boolean; image_caption: boolean }
export interface TypeRule { strategy: 'auto' | 'custom' | 'parent_child'; method: string; chunk_token_num: number; delimiter: string; children_delimiter: string }
export interface DocumentIndexConfig {
  processing: ProcessingConfig
  strategy: 'auto' | 'custom' | 'parent_child' | 'by_file_type'
  enhancements: Partial<Enhancements>
  type_rules: Record<string, TypeRule>
}
export interface LibraryChunk { id: string; content: string; available: boolean; important_keywords: string[]; positions?: number[][] }
export interface LibraryChunkInput { content: string; available: boolean; important_keywords: string[]; insert_before?: string; insert_after?: string }
const root = '/document-libraries'
export const listDocumentLibraries = () => request.get<unknown, DocumentLibrary[]>(root)
/** 创建/编辑知识库载荷：processing 扁平字段 + 策略视图（与文档级 DocumentConfigIn 对齐）。 */
export type LibrarySavePayload = ProcessingConfig & {
  name: string; description: string
  strategy: DocumentIndexConfig['strategy']
  enhancements: DocumentIndexConfig['enhancements']
  type_rules: DocumentIndexConfig['type_rules']
}
export const saveDocumentLibrary = (data: LibrarySavePayload, id?: number) => id
  ? request.put<unknown, DocumentLibrary>(`${root}/${id}`, data)
  : request.post<unknown, DocumentLibrary>(root, data)
export const listLibraryDocuments = (id: number) => request.get<unknown, LibraryDocument[]>(`${root}/${id}/documents`)
export const uploadLibraryDocument = (id: number, file: File) => {
  const data = new FormData(); data.append('file', file)
  return request.post<unknown, LibraryDocument>(`${root}/${id}/documents`, data, {timeout: 180000})
}
export const libraryDocumentAction = (id: number, doc: string, action: 'parse' | 'stop' | 'refresh') =>
  request.post<unknown, LibraryDocument>(`${root}/${id}/documents/${doc}/${action}`, {}, {timeout: 180000})
export const setDocumentEnabled = (id: number, doc: string, enabled: boolean) =>
  request.post<unknown, LibraryDocument>(`${root}/${id}/documents/${doc}/enabled`, {enabled})
export const setDocumentConfig = (id: number, doc: string, config: DocumentIndexConfig) =>
  request.put<unknown, LibraryDocument>(`${root}/${id}/documents/${doc}/config`, config)
export const getLibraryChunks = (id: number, doc: string, page = 1, keywords = '') =>
  request.get<unknown, {chunks: LibraryChunk[]; total: number}>(`${root}/${id}/documents/${doc}/chunks`, {params: {page, size: 20, keywords}})
export const saveLibraryChunk = (id: number, doc: string, data: LibraryChunkInput, chunk?: string) => chunk
  ? request.put(`${root}/${id}/documents/${doc}/chunks/${chunk}`, data)
  : request.post(`${root}/${id}/documents/${doc}/chunks`, data)
export const deleteLibraryChunk = (id: number, doc: string, chunk: string) => request.delete(`${root}/${id}/documents/${doc}/chunks/${chunk}`)
export const deleteLibraryDocument = (id: number, doc: string) => request.delete(`${root}/${id}/documents/${doc}`)
export const getLibraryDocumentPreviewUrl = (id: number, doc: string) =>
  request.get<unknown, {preview_url: string; filename: string}>(`${root}/${id}/documents/${doc}/preview`)
export const setLibraryDocumentTags = (id: number, doc: string, tags: string[]) =>
  request.put<unknown, LibraryDocument>(`${root}/${id}/documents/${doc}/tags`, {tags})
export const downloadOriginal = (id: number, doc: string) => request.get<unknown, Blob>(`${root}/${id}/documents/${doc}/original`, {responseType: 'blob'})
/** 解析原文：format=json 走 JSON 视图，format=markdown 按内容渲染（图片相对引用经代理改写）。 */
export const getLibraryDocumentParsedContent = (id: number, doc: string) =>
  request.get<unknown, {content: string; format: 'json' | 'markdown'}>(`${root}/${id}/documents/${doc}/parsed-content`)

export const listEmbeddingModels = () => request.get<unknown, {id: string; name: string}[]>('/document-libraries/embedding-models')
export const exportDocumentLibrary = (id: number) => request.get<unknown, Blob>(`/document-libraries/${id}/export`, {responseType: 'blob', timeout: 600000})

export const deleteDocumentLibrary = (id: number) => request.delete(`/document-libraries/${id}`)
