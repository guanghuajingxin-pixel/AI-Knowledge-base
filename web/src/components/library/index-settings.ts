/** 索引设置：文档级 / 知识库级共用的策略视图模型与转换逻辑。

策略视图（前端交互）与解析配置（后端 ProcessingConfig）的双向转换：
- auto         自动：chunk_method=auto，分段器内置分层瀑布（标题结构切分 → 递归长度
               修正 → 无结构文本统计兜底），chunk_token_num 作为目标长度
- custom       自定义：分段方式 + 分段标识符（预设多选/自定义）+ 分段最大长度
               + 分段重叠度 + 文本预处理规则
- parent_child 父子分段：naive + enable_children + 子分段标识符
- by_file_type 按文件类型：type_rules 按扩展名覆盖，未命中的走 auto（仅知识库级）
*/
import type { DocumentIndexConfig, Enhancements, ProcessingConfig, TypeRule } from '@/api/document-library'

export type Strategy = 'auto' | 'custom' | 'parent_child' | 'by_file_type'

export interface PreprocessRules { replace_whitespace: boolean; remove_urls_emails: boolean }

export interface IndexSettings {
  strategy: Strategy
  method: string
  chunk_token_num: number
  overlap: number
  delimiter: string
  children_delimiter: string
  layout_recognize: string
  preprocess: PreprocessRules
  type_rules: Record<string, TypeRule>
  enhancements: Enhancements
}

export const METHODS = [
  {value: 'naive', label: '通用文档'}, {value: 'manual', label: '说明书'}, {value: 'paper', label: '论文'},
  {value: 'book', label: '书籍'}, {value: 'laws', label: '法律法规'}, {value: 'presentation', label: '演示文稿'},
  {value: 'table', label: '表格'}, {value: 'one', label: '整篇分段'}]

/** 分段标识符预设（id 与 Dify 分段标识符选项对齐；换行/2个换行切分语义等价，序列化时统一为 \n） */
export const DELIMITER_PRESETS = [
  {id: 'nl', label: '换行', chars: '\n'},
  {id: 'nl2', label: '2个换行', chars: '\n\n'},
  {id: 'zh_period', label: '中文句号', chars: '。'},
  {id: 'zh_excl', label: '中文叹号', chars: '！'},
  {id: 'zh_quest', label: '中文问号', chars: '？'},
  {id: 'en_period', label: '英文句号', chars: '.'},
  {id: 'en_excl', label: '英文叹号', chars: '!'},
  {id: 'en_quest', label: '英文问号', chars: '?'},
  {id: 'ellipsis', label: '省略号', chars: '…'}] as const

export const ENHANCEMENT_DEFAULTS: Enhancements = {
  include_filename: true, auto_summary: true, auto_questions: true, image_caption: true}

export const AUTO_DEFAULTS = {method: 'naive', chunk_token_num: 512, delimiter: '\n。！？；', children_delimiter: '\n'}

const PREPROCESS_DEFAULTS: PreprocessRules = {replace_whitespace: false, remove_urls_emails: false}

export function defaultSettings(): IndexSettings {
  return {strategy: 'auto', ...AUTO_DEFAULTS, overlap: 25, layout_recognize: 'DeepDOC',
          preprocess: {...PREPROCESS_DEFAULTS}, type_rules: {}, enhancements: {...ENHANCEMENT_DEFAULTS}}
}

/** 已保存的文档配置优先；无配置时从库级 processing 推导（父子启用 → parent_child，auto 方法 → auto，否则 custom）。
未配置文档的 config 是空对象 {}（truthy），必须以 cfg?.processing 是否存在判断，否则 strategy 会被写成 undefined。
fallback 兼容两种形态：旧版扁平 ProcessingConfig 与新版库级全量配置（含 processing 键，仅取其 processing）。 */
export function settingsFromConfig(cfg?: DocumentIndexConfig | null,
    fallback?: ProcessingConfig | DocumentIndexConfig | null): IndexSettings {
  const s = defaultSettings()
  const p = cfg?.processing || (fallback && 'processing' in fallback ? fallback.processing : fallback)
  if (p) {
    s.method = p.chunk_method === 'auto' ? 'naive' : p.chunk_method
    s.chunk_token_num = p.chunk_token_num
    s.overlap = p.overlap ?? 25
    s.delimiter = p.delimiter
    s.children_delimiter = p.children_delimiter || '\n'
    s.layout_recognize = p.layout_recognize || 'DeepDOC'
    s.preprocess = {replace_whitespace: p.replace_whitespace ?? false, remove_urls_emails: p.remove_urls_emails ?? false}
  }
  if (cfg?.processing) {
    s.strategy = cfg.strategy
    s.type_rules = cfg.type_rules || {}
    s.enhancements = {...ENHANCEMENT_DEFAULTS, ...(cfg.enhancements || {})}
  } else {
    s.strategy = p?.enable_children ? 'parent_child' : p?.chunk_method === 'auto' ? 'auto' : 'custom'
  }
  return s
}

/** 库级全量配置 → 分段设置（创建/编辑知识库回填）：完整还原策略视图（含按文件类型与检索内容增强），
旧库无 strategy 时按 processing 推导（父子 → parent_child，auto → auto，否则 custom）。 */
export function librarySettingsFromConfig(cfg?: DocumentIndexConfig | null): IndexSettings {
  const s = defaultSettings()
  const p = cfg?.processing
  if (p) {
    s.method = p.chunk_method === 'auto' ? 'naive' : p.chunk_method
    s.chunk_token_num = p.chunk_token_num
    s.overlap = p.overlap ?? 25
    s.delimiter = p.delimiter
    s.children_delimiter = p.children_delimiter || '\n'
    s.layout_recognize = p.layout_recognize || 'DeepDOC'
    s.preprocess = {replace_whitespace: p.replace_whitespace ?? false, remove_urls_emails: p.remove_urls_emails ?? false}
    s.strategy = cfg!.strategy || (p.enable_children ? 'parent_child' : p.chunk_method === 'auto' ? 'auto' : 'custom')
    s.type_rules = cfg!.type_rules || {}
    s.enhancements = {...ENHANCEMENT_DEFAULTS, ...(cfg!.enhancements || {})}
  }
  return s
}

/** 策略视图 → 解析配置：auto 映射 chunk_method=auto（分层瀑布由分段器内置实现，512/默认分隔符作为长度修正参数）；parent_child 固定 naive + enable_children。 */
export function settingsToConfig(s: IndexSettings): DocumentIndexConfig {
  const auto = s.strategy === 'auto'
  const processing: ProcessingConfig = {
    chunk_method: auto ? 'auto' : s.method,
    layout_recognize: s.layout_recognize || 'DeepDOC',
    chunk_token_num: auto ? 512 : s.chunk_token_num,
    delimiter: auto ? '\n。！？；' : s.delimiter,
    overlap: auto ? 25 : s.overlap,
    replace_whitespace: s.preprocess.replace_whitespace,
    remove_urls_emails: s.preprocess.remove_urls_emails,
    embedding_model: '',
    enable_children: s.strategy === 'parent_child',
    children_delimiter: s.children_delimiter,
    auto_keywords: 0,
    auto_questions: 0}
  return {processing, strategy: s.strategy, enhancements: {...s.enhancements}, type_rules: s.type_rules}
}
