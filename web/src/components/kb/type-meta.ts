/**
 * 知识库类型元数据（公共）——筛选标签、卡片图标与配色统一从这里取，
 * 避免配置页/弹窗/列表多处各画各的导致样式漂移。
 */
import { markRaw, type Component } from 'vue'
import { Connection, Document, Platform } from '@element-plus/icons-vue'
import type { KnowledgeLibrary } from '@/api/knowledge-library'

export type KbTypeKey = 'document' | 'dify' | 'ragflow'

export const KB_TYPE_META: Record<KbTypeKey, { label: string; icon: Component; color: string; bg: string }> = {
  document: { label: '文档库', icon: markRaw(Document), color: 'var(--el-color-primary)', bg: 'var(--el-color-primary-light-9)' },
  dify: { label: 'DIFY库', icon: markRaw(Connection), color: '#8b5cf6', bg: '#f4f0ff' },
  ragflow: { label: 'RAGFLOW库', icon: markRaw(Platform), color: '#13c2c2', bg: '#e6fffb' },
}

export function kbTypeKey(lib: KnowledgeLibrary): KbTypeKey {
  if (lib.library_type === 'document') return 'document'
  if (lib.platform === 'dify') return 'dify'
  if (lib.platform === 'ragflow') return 'ragflow'
  return 'document'
}

export function kbTypeMeta(lib: KnowledgeLibrary) {
  return KB_TYPE_META[kbTypeKey(lib)]
}

export function kbTypeLabel(lib: KnowledgeLibrary): string {
  return KB_TYPE_META[kbTypeKey(lib)].label
}
