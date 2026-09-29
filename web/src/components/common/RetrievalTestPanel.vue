<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Search, ArrowDown, Document } from '@element-plus/icons-vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { searchTest } from '@/api/search'
import { testKnowledgeRetrieval, type RetrievalTestHit } from '@/api/knowledge-library'
import { listDocumentLibraries } from '@/api/document-library'
import { retrievalFromConfig } from '@/components/library/index-settings'
import RetrievalSettingsDialog, { type RetrievalSettings } from './RetrievalSettingsDialog.vue'
import { rewriteChunkImages } from '@/utils/chunk-images'

// Markdown 渲染：允许 img（文档库命中分段里的 MinerU 图片按命中文档改写为代理 URL）
const renderMarkdown = (content: string, documentId?: string) => {
  const html = DOMPurify.sanitize(marked.parse(content, { async: false }) as string,
    { FORBID_TAGS: ['iframe', 'video', 'audio'] })
  return props.libraryId && documentId ? rewriteChunkImages(html, props.libraryId, documentId) : html
}

const props = defineProps<{
  /** 后端：local=本地知识库 ES 检索；library=知识库抽象层（Dify/RAGFlow）检索 */
  backend?: 'local' | 'library'
  /** 检索范围：document=单文档内检索；kb=整个知识库检索 */
  scope: 'document' | 'kb'
  /** 本地知识库 ID（backend=local 时必填） */
  kbId?: string
  /** 知识库抽象层 ID（backend=library 时必填） */
  libraryId?: number
  /** 文档 ID / 名称（scope=document 时用于过滤） */
  documentId?: string
  documentTitle?: string
  /** 标题中括号内显示的名称，如文档名 / 知识库名 */
  title?: string
}>()

const backend = computed(() => props.backend || 'library')

// ===== 检索设置（按后端区分模式枚举）=====
const localModes = [
  { value: 'hybrid', label: '混合检索' },
  { value: 'vector', label: '向量检索' },
  { value: 'fulltext', label: '全文检索' },
]
const libraryModes = [
  { value: 'hybrid', label: '混合检索' },
  { value: 'vector', label: '向量检索' },
  { value: 'fulltext', label: '全文检索' },
]
const modeOptions = computed(() => backend.value === 'local' ? localModes : libraryModes)

const settings = reactive<RetrievalSettings>({
  mode: 'hybrid',
  top_k: 8,
  score_threshold: 0,
  rerank: true,
  rerank_model_id: '',
  vector_weight: 0.7,
})

const settingsDialog = ref(false)

const modeLabel = computed(() => {
  const m = modeOptions.value.find(o => o.value === settings.mode)?.label || settings.mode
  return settings.rerank ? `${m}/Rerank` : m
})

// ===== 查询输入 =====
const query = ref('')
const MAX_QUERY = 200
const loading = ref(false)

// ===== 结果（统一结构）=====
interface UnifiedHit {
  rerank_score?: number
  semantic_weight?: number
  score_type?: string
  token_similarity?: number
  vector_similarity?: number
  matched_content?: string
  text: string
  score: number
  document_title: string
  document_id?: string
  chunk_index?: number
  library_name?: string
}
const hits = ref<UnifiedHit[]>([])
const tookMs = ref<number | null>(null)
const libErrors = ref<string[]>([])

async function runSearch() {
  const q = query.value.trim()
  if (!q) { ElMessage.warning('请输入检索内容'); return }
  loading.value = true
  hits.value = []
  libErrors.value = []
  tookMs.value = null
  try {
    const t0 = performance.now()
    let raw: UnifiedHit[] = []

    if (backend.value === 'local') {
      const filters: Record<string, string[]> = {}
      if (props.scope === 'document' && props.documentId) {
        filters.document_ids = [props.documentId]
      }
      const res = await searchTest({
        query: q,
        kb_ids: [props.kbId!],
        top_k: settings.top_k,
        search_type: ({ vector: 'semantic', fulltext: 'keyword', hybrid: 'hybrid' } as const)[settings.mode],
        filters,
        rerank: settings.rerank,
        rerank_model_id: settings.rerank_model_id || undefined,
        score_threshold: settings.score_threshold,
      })
      raw = (res.results || []).map(h => ({
        ...h, text: h.text, score: h.score, document_title: h.document_title,
        document_id: h.document_id, chunk_index: h.chunk_index,
      }))
    } else {
      const res = await testKnowledgeRetrieval({
        query: q,
        library_ids: props.libraryId ? [props.libraryId] : [],
        top_k: settings.top_k,
        mode: settings.mode,
        rerank: settings.rerank,
        rerank_model_id: settings.rerank_model_id || undefined,
        similarity_threshold: settings.score_threshold,
        vector_similarity_weight: settings.mode === 'hybrid' ? settings.vector_weight : undefined,
        document_ids: props.scope === 'document' && props.documentId ? [props.documentId] : undefined,
      })
      libErrors.value = (res.libraries || []).filter(l => !l.ok).map(l => `${l.name}: ${l.error}`)
      raw = (res.hits || []).map((h: RetrievalTestHit) => ({
        ...h, text: h.content, score: h.score, document_title: h.document_title,
        document_id: h.document_id, library_name: h.library_name,
      }))

    }

    tookMs.value = Math.round(performance.now() - t0)
    hits.value = raw.filter(h => (h.score ?? 0) >= settings.score_threshold)
    saveRecord(q)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e.message || '检索失败')
  } finally {
    loading.value = false
  }
}

function scoreLabel(type?: string) {
  return ({ ragflow_token: '词项相关性', cosine: '余弦相似度', ragflow_hybrid: '混合评分',
    ragflow_rerank: '词项与重排加权', ragflow: 'RAGFlow 引擎' } as Record<string, string>)[type || ''] || '引擎评分'
}
function scoreFormula(hit: UnifiedHit) {
  const weight = hit.semantic_weight
  if (weight == null || !['ragflow_hybrid', 'ragflow_rerank'].includes(hit.score_type || '')) return ''
  const semantic = hit.rerank_score ?? hit.vector_similarity
  return `${(1 - weight).toFixed(2)} × 词项 ${hit.token_similarity?.toFixed(6)} + ${weight.toFixed(2)} × ${hit.rerank_score != null ? '重排' : '向量'} ${semantic?.toFixed(6)}`
}

function scorePercent(score: number) {
  return Math.max(0, Math.min(100, score * 100))
}

// ===== 最近测试记录（localStorage，按 backend+scope+目标隔离） =====
interface TestRecord {
  query: string
  mode: string
  top_k: number
  score_threshold: number
  rerank: boolean
  hit_count: number
  timestamp: number
}
const records = ref<TestRecord[]>([])
const recordKey = computed(() => {
  const target = props.scope === 'document' ? (props.documentId || props.documentTitle || 'doc') : (props.libraryId || props.kbId || 'kb')
  return `retrieval_test_records:${backend.value}:${props.scope}:${target}`
})
const MAX_RECORDS = 20

function loadRecords() {
  try {
    const raw = localStorage.getItem(recordKey.value)
    records.value = raw ? JSON.parse(raw) : []
  } catch { records.value = [] }
}
function saveRecord(q: string) {
  const rec: TestRecord = {
    query: q, mode: modeLabel.value, top_k: settings.top_k,
    score_threshold: settings.score_threshold, rerank: settings.rerank,
    hit_count: hits.value.length, timestamp: Date.now(),
  }
  records.value = [rec, ...records.value.filter(r => r.query !== q)].slice(0, MAX_RECORDS)
  try { localStorage.setItem(recordKey.value, JSON.stringify(records.value)) } catch { /* ignore */ }
}
function useRecord(rec: TestRecord) {
  query.value = rec.query
  const baseMode = rec.mode.replace('/Rerank', '')
  const found = modeOptions.value.find(o => o.label === baseMode)
  if (found) settings.mode = found.value as RetrievalSettings['mode']
  settings.rerank = rec.rerank
  settings.top_k = rec.top_k
  settings.score_threshold = rec.score_threshold
  runSearch()
}
function clearRecords() {
  records.value = []
  try { localStorage.removeItem(recordKey.value) } catch { /* ignore */ }
}
function formatTime(ts: number) {
  const d = new Date(ts)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

onMounted(loadRecords)

// 知识库抽象层检索：目标为文档库且已保存「检索设置」时，按库配置预填本次测试初值
// （外部库 Dify/RAGFlow 无本地检索设置，保持全局默认；预填失败静默回退）
onMounted(async () => {
  if (backend.value !== 'library' || !props.libraryId) return
  try {
    const libs = await listDocumentLibraries()
    const lib = libs.find(l => l.id === props.libraryId)
    if (lib?.config?.retrieval) Object.assign(settings, retrievalFromConfig(lib.config.retrieval))
  } catch { /* 预填失败保持默认 */ }
})
</script>

<template>
  <div class="retrieval-test-panel">
    <!-- 头部标题 -->
    <div class="rtp-header">
      <span class="rtp-title">检索测试</span>
      <span v-if="title" class="rtp-subtitle">[{{ title }}]</span>
    </div>

    <div class="rtp-body">
      <!-- 左列：输入区 + 记录区 -->
      <div class="rtp-col rtp-left">
        <!-- 左上：检索内容输入区 -->
        <section class="rtp-card rtp-input-area">
          <div class="rtp-mode-row">
            <el-button class="rtp-mode-btn" @click="settingsDialog = true">
              {{ modeLabel }}
              <el-icon class="el-icon--right"><arrow-down /></el-icon>
            </el-button>
          </div>
          <el-input
            v-model="query"
            type="textarea"
            :rows="8"
            :maxlength="MAX_QUERY"
            show-word-limit
            placeholder="输入检索内容，回车或点击检索"
            @keydown.enter.exact.prevent="runSearch"
          />
          <div class="rtp-input-actions">
            <el-button type="primary" :icon="Search" :loading="loading" @click="runSearch">检索</el-button>
          </div>
        </section>

        <!-- 左下：最近测试记录区 -->
        <section class="rtp-card rtp-records-area">
          <div class="rtp-card-header">
            <span class="rtp-card-title">最近测试</span>
            <span class="rtp-card-hint">仅展示最近 {{ MAX_RECORDS }} 条，点击单条可重新查询</span>
            <div class="rtp-card-spacer" />
            <el-button v-if="records.length" link type="danger" @click="clearRecords">清空</el-button>
          </div>
          <div class="rtp-records-list">
            <div v-if="!records.length" class="rtp-empty">暂无测试记录</div>
            <div
              v-for="(rec, i) in records"
              :key="i"
              class="rtp-record-item"
              @click="useRecord(rec)"
            >
              <div class="rtp-record-top">
                <el-tag size="small" type="info" effect="plain">{{ rec.mode }}</el-tag>
                <span class="rtp-record-query">{{ rec.query }}</span>
                <span class="rtp-record-time">{{ formatTime(rec.timestamp) }}</span>
              </div>
            </div>
          </div>
        </section>
      </div>

      <!-- 右列：设置区 + 结果区 -->
      <div class="rtp-col rtp-right">
        <!-- 右上：检索设置显示区 -->
        <section class="rtp-card rtp-settings-area">
          <div class="rtp-card-header">
            <span class="rtp-card-title">检索设置</span>
          </div>
          <div class="rtp-settings-grid">
            <div class="rtp-setting-item">
              <label>检索模式</label>
              <div class="rtp-setting-value">{{ modeLabel }}</div>
            </div>
            <div class="rtp-setting-item">
              <label>TopK</label>
              <el-input-number
                v-model="settings.top_k"
                :min="1"
                :max="50"
                size="small"
                controls-position="right"
              />
            </div>
            <div class="rtp-setting-item">
              <label>Score 阈值</label>
              <el-input-number
                v-model="settings.score_threshold"
                :min="0"
                :max="1"
                :step="0.05"
                :precision="2"
                size="small"
                controls-position="right"
              />
            </div>
          </div>
        </section>

        <!-- 右下：测试结果显示区 -->
        <section class="rtp-card rtp-results-area">
          <div class="rtp-card-header">
            <span class="rtp-card-title">测试结果</span>
            <span class="rtp-card-hint">按实际评分排序；Score 表示相关性，不代表正确率</span>
            <div class="rtp-card-spacer" />
            <span v-if="tookMs !== null" class="rtp-took">({{ (tookMs / 1000).toFixed(3) }}s)</span>
          </div>
          <div class="rtp-results-list">
            <el-alert
              v-for="(err, i) in libErrors"
              :key="i"
              :title="err"
              type="error"
              :closable="false"
              show-icon
              style="margin-bottom: 8px"
            />
            <el-empty v-if="!loading && !hits.length" description="暂无检索结果" :image-size="80" />
            <div v-loading="loading" class="rtp-hits">
              <article v-for="(hit, i) in hits" :key="i" class="rtp-hit">
                <div class="rtp-hit-head">
                  <span class="rtp-hit-rank">#{{ i + 1 }}</span>
                  <span class="rtp-hit-rank-label">综合排名</span>
                  <div class="rtp-card-spacer" />
                  <span class="rtp-hit-score-label">Score · {{ scoreLabel(hit.score_type) }}</span>
                  <span class="rtp-hit-score">{{ hit.score?.toFixed(6) }}</span>
                </div>
                <div class="rtp-hit-score-bar">
                  <div class="rtp-hit-score-fill" :style="{ width: scorePercent(hit.score || 0) + '%' }" />
                </div>
                <div class="rtp-card-hint" v-if="hit.token_similarity != null || hit.vector_similarity != null">
                  词项 {{ hit.token_similarity?.toFixed(6) ?? '—' }} · 向量 {{ hit.vector_similarity?.toFixed(6) ?? '—' }}
                </div>
                <div v-if="scoreFormula(hit)" class="rtp-card-hint">{{ scoreFormula(hit) }}</div>
                <details v-if="hit.matched_content && hit.matched_content !== hit.text">
                  <summary>查看实际评分的命中子分段</summary>
                  <div class="rtp-hit-content rtp-md" v-html="renderMarkdown(hit.matched_content, hit.document_id)" />
                </details>
                <div class="rtp-hit-content rtp-md" v-html="renderMarkdown(hit.text, hit.document_id)" />
                <div class="rtp-hit-meta">
                  <el-icon><document /></el-icon>
                  <span class="rtp-hit-doc">{{ hit.document_title }}</span>
                  <span v-if="hit.chunk_index !== undefined" class="rtp-hit-chunk">分段 #{{ hit.chunk_index + 1 }}</span>
                </div>
              </article>
            </div>
          </div>
        </section>
      </div>
    </div>

    <RetrievalSettingsDialog
      v-model="settingsDialog"
      :settings="settings"
      @save="(s) => Object.assign(settings, s)"
    />
  </div>
</template>

<style scoped>
.retrieval-test-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.rtp-header {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  margin-bottom: 16px;
  flex-shrink: 0;
}
.rtp-title { font-size: 16px; font-weight: 600; color: var(--el-text-color-primary); }
.rtp-subtitle { font-size: 13px; color: var(--el-text-color-secondary); }

.rtp-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: row;
  gap: 16px;
}
.rtp-col { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.rtp-left { flex: 0 0 45%; gap: 16px; }
.rtp-right { flex: 1; gap: 16px; }

.rtp-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: #fff;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.rtp-input-area { flex-shrink: 0; padding: 14px; gap: 10px; }
.rtp-records-area { flex: 1; min-height: 0; padding: 14px; }
.rtp-settings-area { flex-shrink: 0; padding: 14px; }
.rtp-results-area { flex: 1; min-height: 0; padding: 14px; }

.rtp-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  flex-shrink: 0;
}
.rtp-card-title { font-size: 14px; font-weight: 600; color: var(--el-text-color-primary); }
.rtp-card-hint { font-size: 12px; color: var(--el-text-color-secondary); }
.rtp-card-spacer { flex: 1; }
.rtp-took { font-size: 12px; color: var(--el-text-color-secondary); }

/* 输入区 */
.rtp-mode-row { display: flex; align-items: center; margin-bottom: 8px; }
.rtp-mode-btn { padding: 5px 12px; }
.rtp-rerank { display: flex; align-items: center; }
.rtp-input-actions { display: flex; justify-content: flex-end; }

/* 设置区 */
.rtp-settings-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
.rtp-setting-item { display: flex; flex-direction: column; gap: 6px; }
.rtp-setting-item label { font-size: 12px; color: var(--el-text-color-secondary); }
.rtp-setting-value { font-size: 14px; font-weight: 500; color: var(--el-text-color-primary); }

/* 记录区 */
.rtp-records-list { flex: 1; min-height: 0; overflow-y: auto; }
.rtp-empty { padding: 24px 0; text-align: center; color: var(--el-text-color-placeholder); font-size: 13px; }
.rtp-record-item {
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s;
}
.rtp-record-item:hover { background: var(--el-fill-color-light); }
.rtp-record-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.rtp-record-query {
  flex: 1;
  font-size: 13px;
  color: var(--el-text-color-regular);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.rtp-record-time { font-size: 11px; color: var(--el-text-color-placeholder); flex-shrink: 0; }

/* 结果区 */
.rtp-results-list { flex: 1; min-height: 0; overflow-y: auto; }
.rtp-hit {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 12px;
  margin-bottom: 10px;
}
.rtp-hit-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.rtp-hit-rank {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-size: 12px;
  font-weight: 600;
}
.rtp-hit-rank-label { font-size: 12px; color: var(--el-text-color-secondary); }
.rtp-hit-score-label { font-size: 12px; color: var(--el-text-color-secondary); }
.rtp-hit-score { font-size: 12px; color: var(--el-text-color-regular); font-family: monospace; }
.rtp-hit-score-bar {
  height: 4px;
  background: var(--el-fill-color);
  border-radius: 2px;
  overflow: hidden;
  margin-bottom: 8px;
}
.rtp-hit-score-fill {
  height: 100%;
  background: var(--el-color-primary);
  border-radius: 2px;
  transition: width 0.3s;
}
.rtp-hit-content {
  font-size: 13px;
  line-height: 1.7;
  color: var(--el-text-color-regular);
  word-break: break-word;
  overflow-wrap: anywhere;
  margin-bottom: 8px;
}
/* Markdown 排版 */
.rtp-md :deep(h1), .rtp-md :deep(h2), .rtp-md :deep(h3), .rtp-md :deep(h4) {
  font-weight: 600; color: var(--el-text-color-primary); margin: 12px 0 6px;
}
.rtp-md :deep(h1) { font-size: 18px; }
.rtp-md :deep(h2) { font-size: 16px; }
.rtp-md :deep(h3) { font-size: 14px; }
.rtp-md :deep(p) { margin: 4px 0; }
.rtp-md :deep(ul), .rtp-md :deep(ol) { padding-left: 20px; margin: 4px 0; }
.rtp-md :deep(li) { margin: 2px 0; }
.rtp-md :deep(table) { border-collapse: collapse; width: 100%; margin: 6px 0; font-size: 12px; }
.rtp-md :deep(th), .rtp-md :deep(td) {
  border: 1px solid var(--el-border-color-lighter); padding: 4px 8px; text-align: left;
}
.rtp-md :deep(th) { background: var(--el-fill-color-lighter); font-weight: 600; }
.rtp-md :deep(code) {
  background: var(--el-fill-color-light); padding: 1px 5px; border-radius: 3px;
  font-size: 12px; font-family: var(--el-font-family);
}
.rtp-md :deep(pre) {
  background: var(--el-fill-color-lighter); padding: 8px; border-radius: 4px;
  overflow-x: auto; margin: 6px 0;
}
.rtp-md :deep(blockquote) {
  margin: 6px 0; padding: 4px 12px; border-left: 3px solid var(--el-border-color);
  color: var(--el-text-color-secondary); background: var(--el-fill-color-lighter);
}
.rtp-md :deep(img) { max-width: 100%; border-radius: 4px; margin: 6px 0; display: block; }
.rtp-md :deep(a) { color: var(--el-color-primary); text-decoration: none; }
.rtp-md :deep(a:hover) { text-decoration: underline; }
.rtp-md :deep(hr) { border: none; border-top: 1px solid var(--el-border-color-lighter); margin: 8px 0; }
.rtp-md :deep(strong) { font-weight: 600; }
.rtp-hit-meta { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--el-text-color-secondary); }
.rtp-hit-doc { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rtp-hit-chunk { color: var(--el-text-color-placeholder); }
</style>
