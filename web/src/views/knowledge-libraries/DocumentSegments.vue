<script setup lang="ts">
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Back, Search, Close, View, Top, Bottom, Edit, Delete } from '@element-plus/icons-vue'
import { listDocumentLibraries, listLibraryDocuments, getLibraryChunks, saveLibraryChunk, deleteLibraryChunk, getLibraryDocumentPreviewUrl, type DocumentLibrary, type LibraryDocument, type LibraryChunk, type LibraryChunkInput } from '@/api/document-library'
import { rewriteChunkImages } from '@/utils/chunk-images'
import { useTabsStore } from '@/stores/tabs'
import ParsedContentView from '@/components/common/ParsedContentView.vue'

const route = useRoute()
const router = useRouter()
const tabsStore = useTabsStore()
const libId = Number(route.params.libId)
const docId = route.params.docId as string

const lib = ref<DocumentLibrary>()
const doc = ref<LibraryDocument>()
const docName = ref(String(route.query.name || ''))
const statusLabels: Record<string, string> = {PARSING: '解析中', COMPLETED: '已完成', FAILED: '失败', CANCELLED: '已停止', UNKNOWN: '状态未知'}

const chunks = ref<LibraryChunk[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const keyword = ref('')
// 分段渲染：允许 img（MinerU 图片经 rewriteChunkImages 改写为带鉴权代理 URL）
const renderChunk = (content: string) => highlightKeyword(rewriteChunkImages(
  DOMPurify.sanitize(marked.parse(content, {async: false}) as string, {FORBID_TAGS: ['iframe', 'video', 'audio']}),
  libId, docId))

// 搜索命中高亮：只处理渲染后 HTML 的文本节点（不碰标签/属性），关键词包 <mark>
const escapeHtml = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
function highlightKeyword(html: string): string {
  const kw = keyword.value.trim()
  if (!kw) return html
  const escapedKw = escapeHtml(kw)
  const re = new RegExp(`(${escapedKw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi')
  const lower = escapedKw.toLowerCase()
  const container = document.createElement('div')
  container.innerHTML = html
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT)
  const nodes: Text[] = []
  while (walker.nextNode()) {
    if (escapeHtml(walker.currentNode.nodeValue || '').toLowerCase().includes(lower))
      nodes.push(walker.currentNode as Text)
  }
  for (const node of nodes) {
    const span = document.createElement('span')
    span.innerHTML = escapeHtml(node.nodeValue || '').replace(re, '<mark>$1</mark>')
    node.parentNode?.replaceChild(span, node)
  }
  return container.innerHTML
}

// 源文件预览面板 / 解析原文面板（右侧，二者互斥避免三栏挤压）
const previewVisible = ref(false)
const previewUrl = ref('')
const previewLoading = ref(false)
const parsedVisible = ref(false)
const sidePanelVisible = computed(() => previewVisible.value || parsedVisible.value)
async function togglePreview() {
  if (previewVisible.value) { previewVisible.value = false; return }
  previewLoading.value = true
  try {
    const res = await getLibraryDocumentPreviewUrl(libId, docId)
    previewUrl.value = res.preview_url
    parsedVisible.value = false
    previewVisible.value = true
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e.message || '获取预览地址失败')
  } finally {
    previewLoading.value = false
  }
}
function toggleParsed() {
  parsedVisible.value = !parsedVisible.value
  if (parsedVisible.value) previewVisible.value = false
}
function openPreviewInNewTab() {
  if (previewUrl.value) window.open(previewUrl.value, '_blank')
}
// 解析原文新页签查看（路由导航自动开页签，页面从 sessionStorage 取文档名）
function goParsedContent() {
  if (docName.value) sessionStorage.setItem(`doc_name:${docId}`, docName.value)
  router.push({ path: `/apply/knowledge-libraries/${libId}/documents/${docId}/parsed-content` })
}

// 检索测试：新页签打开
function goRetrievalTest() {
  if (docName.value) sessionStorage.setItem(`doc_name:${docId}`, docName.value)
  router.push({ path: `/apply/knowledge-libraries/${libId}/documents/${docId}/retrieval-test` })
}

const chunkEdit = ref(false)
const chunkId = ref<string>()
const insertRef = ref<{id: string; where: 'before' | 'after'}>()
const chunkForm = reactive({content: '', available: true})
const chunkKeywords = ref<string[]>([])
const chunkDialogTitle = computed(() =>
  chunkId.value ? '编辑分段'
    : insertRef.value?.where === 'before' ? '向前插入分段'
    : insertRef.value?.where === 'after' ? '向后插入分段'
    : '新增分段')

async function load() {
  loading.value = true
  try { const res = await getLibraryChunks(libId, docId, page.value, keyword.value); chunks.value = res.chunks; total.value = res.total }
  catch (e: any) { error.value = e?.response?.data?.detail || e.message || '加载失败' }
  finally { loading.value = false }
}
async function loadMeta() {
  try {
    const [libs, docs] = await Promise.all([listDocumentLibraries(), listLibraryDocuments(libId)])
    lib.value = libs.find(l => l.id === libId)
    doc.value = docs.find(d => d.id === docId)
    if (doc.value?.name) {
      docName.value = doc.value.name
      tabsStore.updateTabTitle(route.path, doc.value.name)
    }
  } catch { /* 名称回退路由 query */ }
}
function editChunk(chunk?: LibraryChunk) {
  chunkId.value = chunk?.id
  insertRef.value = undefined
  Object.assign(chunkForm, {content: chunk?.content || '', available: chunk?.available ?? true})
  chunkKeywords.value = chunk?.important_keywords || []
  chunkEdit.value = true
}
// 在目标分段前/后插入：打开空白编辑窗，保存时携带插入位置
function insertChunk(chunk: LibraryChunk, where: 'before' | 'after') {
  chunkId.value = undefined
  insertRef.value = {id: chunk.id, where}
  Object.assign(chunkForm, {content: '', available: true})
  chunkKeywords.value = []
  chunkEdit.value = true
}
async function saveChunk() {
  if (!chunkForm.content.trim()) { ElMessage.warning('请输入分段正文'); return }
  busy.value = true; error.value = ''
  try {
    const payload: LibraryChunkInput = {content: chunkForm.content, available: chunkForm.available, important_keywords: chunkKeywords.value}
    if (!chunkId.value && insertRef.value) payload[insertRef.value.where === 'before' ? 'insert_before' : 'insert_after'] = insertRef.value.id
    await saveLibraryChunk(libId, docId, payload, chunkId.value)
    chunkEdit.value = false; insertRef.value = undefined; await load(); ElMessage.success('分段已保存')
  } catch (e: any) { error.value = e?.response?.data?.detail || e.message || '保存失败' }
  finally { busy.value = false }
}
// 启用/停用开关直接作用于分段卡片（等效更新 available，不打开编辑窗）
async function toggleChunk(chunk: LibraryChunk) {
  busy.value = true; error.value = ''
  try {
    await saveLibraryChunk(libId, docId, {content: chunk.content, available: !chunk.available, important_keywords: chunk.important_keywords || []}, chunk.id)
    chunk.available = !chunk.available
  } catch (e: any) { error.value = e?.response?.data?.detail || e.message || '操作失败，请重试' }
  finally { busy.value = false }
}
async function removeChunk(chunk: LibraryChunk) {
  try { await ElMessageBox.confirm('删除该分段？删除后将不再参与检索。', '删除分段', {type: 'warning'}) } catch { return }
  busy.value = true; error.value = ''
  try { await deleteLibraryChunk(libId, docId, chunk.id); await load() }
  catch (e: any) { error.value = e?.response?.data?.detail || e.message || '删除失败' }
  finally { busy.value = false }
}
function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function exportChunks() {
  busy.value = true; error.value = ''
  try {
    const all: LibraryChunk[] = []
    let p = 1
    while (true) {
      const result = await getLibraryChunks(libId, docId, p++)
      all.push(...result.chunks)
      if (all.length >= result.total) break
      if (!result.chunks.length) throw new Error('分段数量发生变化，请刷新后重试导出')
    }
    const meta = doc.value || {id: docId, name: docName.value}
    download(new Blob([JSON.stringify({schema_version: 1, document: meta, processing: lib.value?.config?.processing, chunks: all}, null, 2)], {type: 'application/json'}), `${meta.name}.chunks.json`)
  } catch (e: any) { error.value = e?.response?.data?.detail || e.message || '导出失败' }
  finally { busy.value = false }
}
onMounted(() => { loadMeta(); load() })
</script>
<template>
  <div class="kge-page document-segments">
    <el-alert v-if="error" :title="String(error)" type="error" show-icon @close="error = ''" />
    <div class="content" :class="{'with-preview': sidePanelVisible}" v-loading="loading">
      <!-- 左侧：分段列表 -->
      <div class="main-col">
        <div class="toolbar">
          <el-button link :icon="Back" @click="router.push({path: '/apply/knowledge-libraries', query: {libId: String(libId)}})">返回</el-button>
          <span class="doc-name" :title="docName">{{ docName }}</span>
          <el-tag v-if="doc" :type="doc.status === 'COMPLETED' ? 'success' : doc.status === 'FAILED' ? 'danger' : 'info'">{{ statusLabels[doc.status] || doc.status }}</el-tag>
          <span class="hint">共 {{ total }} 个分段</span>
          <div class="toolbar-spacer" />
          <el-input v-model="keyword" :prefix-icon="Search" placeholder="搜索分段内容" clearable style="width: 220px" @change="page = 1; load()" />
          <el-button :loading="busy" @click="exportChunks">导出全部分段</el-button>
          <el-button :icon="View" :type="previewVisible ? 'primary' : ''" :loading="previewLoading" @click="togglePreview">
            {{ previewVisible ? '关闭源文件' : '查看源文件' }}
          </el-button>
          <el-button :icon="View" :type="parsedVisible ? 'primary' : ''" @click="toggleParsed">
            {{ parsedVisible ? '隐藏解析原文' : '查看解析原文' }}
          </el-button>
          <el-button :icon="Search" @click="goRetrievalTest">检索测试</el-button>
        </div>
        <div class="chunks">
          <el-empty v-if="!chunks.length && !loading" description="暂无分段" />
          <article v-for="(chunk, i) in chunks" :key="chunk.id" class="chunk">
            <div class="chunk-toolbar">
              <div class="chunk-meta">
                <span class="chunk-index">分段 {{ (page - 1) * 20 + i + 1 }}</span>
                <span class="chunk-length">{{ chunk.content.length }} 字符</span>
              </div>
              <div class="toolbar-spacer" />
              <div class="chunk-actions">
                <el-tooltip content="向前插入分段" placement="top">
                  <el-button text type="primary" :icon="Top" :disabled="busy" @click="insertChunk(chunk, 'before')" />
                </el-tooltip>
                <el-tooltip content="向后插入分段" placement="top">
                  <el-button text type="primary" :icon="Bottom" :disabled="busy" @click="insertChunk(chunk, 'after')" />
                </el-tooltip>
                <el-tooltip content="编辑" placement="top">
                  <el-button text type="primary" :icon="Edit" :disabled="busy" @click="editChunk(chunk)" />
                </el-tooltip>
                <el-tooltip content="删除" placement="top">
                  <el-button text type="danger" :icon="Delete" :disabled="busy" @click="removeChunk(chunk)" />
                </el-tooltip>
              </div>
              <el-divider direction="vertical" />
              <div class="switch-line"><el-switch :model-value="chunk.available" :disabled="busy" :aria-label="`分段${(page - 1) * 20 + i + 1}检索状态`" @change="toggleChunk(chunk)" /><span>{{ chunk.available ? '已启用' : '已停用' }}</span></div>
            </div>
            <div class="chunk-content" v-html="renderChunk(chunk.content)" />
            <div v-if="chunk.important_keywords?.length" class="hint">关键词：{{ chunk.important_keywords.join('、') }}</div>
          </article>
        </div>
        <el-pagination v-if="total > 20" :current-page="page" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="p => { page = p; load() }" />
      </div>

      <!-- 右侧：源文件预览面板 -->
      <aside v-if="previewVisible" class="preview-col">
        <header class="preview-header">
          <div class="preview-title">
            <el-tag size="small">源文件</el-tag>
            <span class="preview-filename" :title="docName">{{ docName }}</span>
          </div>
          <div class="preview-actions">
            <el-button link type="primary" @click="openPreviewInNewTab">前往新页面预览 →</el-button>
            <el-button link :icon="Close" aria-label="关闭预览" @click="previewVisible = false" />
          </div>
        </header>
        <div class="preview-body">
          <iframe v-if="previewUrl" :src="previewUrl" class="preview-frame" frameborder="0" />
          <el-empty v-else description="加载预览中..." />
        </div>
      </aside>

      <!-- 右侧：解析原文面板（JSON 内容格式化展示，markdown 按内容渲染） -->
      <aside v-if="parsedVisible" class="preview-col">
        <header class="preview-header">
          <div class="preview-title">
            <el-tag size="small">解析原文</el-tag>
            <span class="preview-filename" :title="docName">{{ docName }}</span>
          </div>
          <div class="preview-actions">
            <el-button link type="primary" @click="goParsedContent">前往新页面查看 →</el-button>
            <el-button link :icon="Close" aria-label="隐藏解析原文" @click="parsedVisible = false" />
          </div>
        </header>
        <div class="preview-body">
          <ParsedContentView :lib-id="libId" :doc-id="docId" />
        </div>
      </aside>
    </div>
    <el-dialog v-model="chunkEdit" :title="chunkDialogTitle" width="min(680px, 94vw)" :close-on-click-modal="false">
      <el-form label-position="top"><el-form-item label="分段正文" required><el-input v-model="chunkForm.content" type="textarea" :rows="12" /></el-form-item></el-form>
      <template #footer><el-button @click="chunkEdit = false">取消</el-button><el-button type="primary" :loading="busy" @click="saveChunk">保存</el-button></template>
    </el-dialog>
  </div>
</template>
<style scoped>
.document-segments .content {
  background: white; padding: 20px; border-radius: 8px;
  flex: 1; min-height: 0; display: flex; flex-direction: row; gap: 16px;
}
.main-col { flex: 1; min-width: 0; display: flex; flex-direction: column; min-height: 0; }
.with-preview .main-col { max-width: calc(50% - 8px); }

.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.toolbar-spacer { flex: 1; min-width: 12px; }
.doc-name { font-size: 15px; font-weight: 600; color: #303133; max-width: 32%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hint { color: #909399; font-size: 13px; }
.switch-line { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.switch-line span { color: var(--el-text-color-regular); font-size: 13px; }
.el-alert { margin-bottom: 16px; }
.chunks { flex: 1; min-height: 0; overflow: auto; }
.chunk { border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 16px; margin-bottom: 12px; }
.chunk-toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
.chunk-meta { display: flex; align-items: center; gap: 10px; }
.chunk-index {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  padding: 2px 10px;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 600;
  line-height: 18px;
}
.chunk-length { color: var(--el-text-color-secondary); font-size: 12px; }
.chunk-actions { display: flex; align-items: center; gap: 2px; }
.chunk-actions .el-button { padding: 6px; }
.chunk-actions .el-button .el-icon { font-size: 16px; }
.chunk-content { overflow-wrap: anywhere; overflow: auto; line-height: 1.7; }
.chunk-content :deep(mark) { background: var(--el-color-warning-light-8, #f3d19e); color: inherit; padding: 0 2px; border-radius: 2px; }
.chunk-content :deep(table) { border-collapse: collapse; width: 100%; }
.chunk-content :deep(td), .chunk-content :deep(th) { border: 1px solid var(--el-border-color-lighter); padding: 6px; }
.chunk-content :deep(img) { max-width: 100%; border-radius: 4px; margin: 6px 0; display: block; }
.el-pagination { margin-top: 16px; justify-content: flex-end; }

/* 右侧预览面板 */
.preview-col {
  flex: 1; min-width: 0; display: flex; flex-direction: column;
  border: 1px solid #ebeef5; border-radius: 8px; overflow: hidden;
  background: #fafafa;
}
.preview-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 14px; background: #fff; border-bottom: 1px solid #ebeef5;
  flex-shrink: 0;
}
.preview-title { display: flex; align-items: center; gap: 10px; min-width: 0; }
.preview-filename {
  font-size: 14px; font-weight: 500; color: #303133;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.preview-actions { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
.preview-body { flex: 1; min-height: 0; background: #fafafa; }
.preview-frame { width: 100%; height: 100%; border: 0; display: block; }
</style>
