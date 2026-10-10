<script setup lang="ts">
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, Close, Top, Bottom, Edit, Delete, QuestionFilled, ArrowDown, Setting } from '@element-plus/icons-vue'
import { listDocumentLibraries, listLibraryDocuments, getLibraryChunks, saveLibraryChunk, deleteLibraryChunk, getLibraryDocumentPreviewUrl, setDocumentConfig, libraryDocumentAction, type DocumentLibrary, type LibraryDocument, type LibraryChunk, type LibraryChunkInput } from '@/api/document-library'
import IndexSettingsDialog from '@/components/library/IndexSettingsDialog.vue'
import { settingsFromConfig, settingsToConfig, type IndexSettings } from '@/components/library/index-settings'
import { fileIcon } from '@/utils/file-icon'
import { rewriteChunkImages } from '@/utils/chunk-images'
import { useTabsStore } from '@/stores/tabs'
import ParsedContentView from '@/components/common/ParsedContentView.vue'
import ChunkEnhancements from '@/components/library/ChunkEnhancements.vue'

const route = useRoute()
const router = useRouter()
const tabsStore = useTabsStore()
const libId = Number(route.params.libId)
const docId = route.params.docId as string

const lib = ref<DocumentLibrary>()
const doc = ref<LibraryDocument>()
const docName = ref(String(route.query.name || ''))

// 索引设置：与文档列表页共用同一弹窗（文档级分段/预处理设置）
const settingsVisible = ref(false)
const settingsValue = ref<IndexSettings>()
const settingsBusy = ref(false)
function openSettings() {
  if (!doc.value) return
  settingsValue.value = settingsFromConfig(doc.value.config, lib.value?.config)
  settingsVisible.value = true
}
async function saveSettings(v: IndexSettings, reparse: boolean) {
  if (!doc.value) return
  settingsBusy.value = true
  try {
    const updated = await setDocumentConfig(libId, docId, settingsToConfig(v))
    Object.assign(doc.value, updated)
    settingsVisible.value = false
    if (reparse) {
      await libraryDocumentAction(libId, docId, 'parse')
      ElMessage.success('设置已保存，正在按新设置重新分段')
      setTimeout(load, 800)
    } else {
      ElMessage.success('索引设置已保存，重新解析后生效')
    }
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || e.message || '保存失败') }
  finally { settingsBusy.value = false }
}

// 「更多」下拉：导出分段 / 源文件预览 / 解析原文预览
function handleToolCommand(cmd: string | number | object) {
  if (cmd === 'export') exportChunks()
  else if (cmd === 'source') togglePreview()
  else if (cmd === 'parsed') toggleParsed()
}

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

// 父子分段模式判定：全文父块（parent_mode=fulltext）时父分段内容与解析原文重复，
// 卡片内不展示父内容、直接平铺子分段；段落模式保持「父内容 + 可展开子分区」
const segSettings = computed(() => settingsFromConfig(doc.value?.config, lib.value?.config))
const fulltextParent = computed(() =>
  segSettings.value.strategy === 'parent_child' && segSettings.value.parent_mode === 'fulltext')

// 子分段展示（父子分段-段落模式）：默认全部收起，点击展开/收起（父卡片独立状态）
const expandedChildren = reactive<Record<string, boolean>>({})
function toggleChildren(chunk: LibraryChunk) { expandedChildren[chunk.id] = !expandedChildren[chunk.id] }

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
      // 页签标题固定「分段详情」，多开时自动编号 -1/-2…，避免长文档名撑爆页签
      tabsStore.assignSequentialTitle(route.path, '分段详情')
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
          <img class="file-ico" :src="fileIcon(docName)" alt="" />
          <span class="doc-name" :title="docName">{{ docName }}</span>
          <span class="hint">共 {{ total }} 个分段</span>
          <div class="toolbar-spacer" />
          <el-input v-model="keyword" :prefix-icon="Search" placeholder="搜索分段内容" clearable style="width: 220px" @change="page = 1; load()" />
          <el-button :icon="Setting" :disabled="!doc || doc.status === 'PARSING'" @click="openSettings">索引设置</el-button>
          <el-button :icon="Search" @click="goRetrievalTest">检索测试</el-button>
          <el-dropdown trigger="click" placement="bottom-end" @command="handleToolCommand">
            <el-button>更多<el-icon class="el-icon--right"><ArrowDown /></el-icon></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="export">导出全部分段</el-dropdown-item>
                <el-dropdown-item command="source">{{ previewVisible ? '关闭源文件' : '查看源文件' }}</el-dropdown-item>
                <el-dropdown-item command="parsed">{{ parsedVisible ? '隐藏解析原文' : '查看解析原文' }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
        <div class="chunks">
          <el-empty v-if="!chunks.length && !loading" description="暂无分段" />
          <article v-for="(chunk, i) in chunks" :key="chunk.id" class="chunk">
            <div class="chunk-toolbar">
              <div class="chunk-meta">
                <span class="chunk-tag" :aria-label="`分段 ${(page - 1) * 20 + i + 1}`">分段 {{ (page - 1) * 20 + i + 1 }}</span>
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
              <el-divider direction="vertical" class="chunk-divider" />
              <div class="switch-line"><el-switch :model-value="chunk.available" :disabled="busy" :aria-label="`分段${(page - 1) * 20 + i + 1}检索状态`" @change="toggleChunk(chunk)" /><span>{{ chunk.available ? '已启用' : '已停用' }}</span></div>
            </div>
            <!-- 段落模式：分段号在 meta 标签中；全文父块模式不展示父内容（与解析原文重复） -->
            <div v-if="!fulltextParent" class="chunk-content" v-html="renderChunk(chunk.content)" />
            <ChunkEnhancements :data="chunk.retrieval_enhancements" />
            <!-- 子分段（父子分段模式）：子块是检索单元，命中后返回父分段上下文；
                 全文父块模式直接平铺全部子分段，段落模式默认收起、点击展开 -->
            <div v-if="chunk.children?.length" class="child-chunks" :class="{main: fulltextParent}">
              <div class="child-head">
                <button v-if="!fulltextParent" type="button" class="child-toggle" :aria-expanded="!!expandedChildren[chunk.id]" @click="toggleChildren(chunk)">
                  <el-icon class="child-caret" :class="{open: !!expandedChildren[chunk.id]}"><ArrowDown /></el-icon>
                  子分段
                </button>
                <span v-else class="child-title">子分段</span>
                <span class="child-count">{{ chunk.children.length }} 个 · 用于检索命中</span>
                <el-tooltip content="子分段由解析按父子分段规则生成：向量与词项打分作用于子分段，命中后返回父分段作为上下文。编辑父分段内容不会同步子分段，重新解析后重建。" placement="top">
                  <el-icon class="child-tip"><QuestionFilled /></el-icon>
                </el-tooltip>
              </div>
              <template v-if="fulltextParent || expandedChildren[chunk.id]">
                <div v-for="(child, j) in chunk.children" :key="child.id" class="child-item">
                  <span class="child-index">{{ j + 1 }}</span>
                  <div class="child-content-wrap">
                    <div class="child-content" v-html="renderChunk(child.content)" />
                    <ChunkEnhancements :data="child.retrieval_enhancements" />
                  </div>
                </div>
              </template>
            </div>
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
    <IndexSettingsDialog v-model="settingsVisible" title="索引设置" scope="document" :value="settingsValue || null" @confirm="saveSettings" />
  </div>
</template>
<style scoped>
.document-segments .content {
  background: white; padding: 20px; border-radius: 8px;
  flex: 1; min-height: 0; display: flex; flex-direction: row; gap: 16px;
}
.main-col { flex: 1; min-width: 0; display: flex; flex-direction: column; min-height: 0; }
.with-preview .main-col { max-width: calc(50% - 8px); }

.toolbar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 16px; }
/* 收紧工具栏按钮间距：Element 默认相邻按钮 12px，压缩为 3px（加 gap 6px = 9px） */
.toolbar :deep(.el-button + .el-button) { margin-left: 3px; }
.file-ico { width: 20px; height: 20px; flex: none; object-fit: contain; }
.toolbar-spacer { flex: 1; min-width: 12px; }
.doc-name { font-size: 15px; font-weight: 600; color: #303133; max-width: 32%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hint { color: #909399; font-size: 13px; }
.switch-line { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.switch-line span { color: var(--el-text-color-regular); font-size: 13px; }
.el-alert { margin-bottom: 16px; }
.chunks { flex: 1; min-height: 0; overflow: auto; }
.chunk { border: 1px solid #e7ebf0; border-radius: 10px; padding: 14px 16px; margin-bottom: 12px; background: #fff; }
.chunk-toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.chunk-meta { display: flex; align-items: center; gap: 10px; min-width: 0; }
/* 分段号：蓝色轻底标签（对齐参考稿的「父分段 2」样式） */
.chunk-tag {
  flex: none; padding: 1px 8px; border-radius: 4px;
  background: var(--el-color-primary-light-9); color: var(--el-color-primary);
  font-size: 12px; font-weight: 600; line-height: 20px;
}
.chunk-length { color: var(--el-text-color-secondary); font-size: 12px; white-space: nowrap; }
/* 操作按钮：悬停整卡时浮现，减少行内元素噪音（开关状态始终可见） */
.chunk-actions { display: flex; align-items: center; gap: 2px; opacity: 0; transition: opacity .15s ease; }
.chunk:hover .chunk-actions, .chunk:focus-within .chunk-actions { opacity: 1; }
.chunk-divider { opacity: 0; transition: opacity .15s ease; }
.chunk:hover .chunk-divider, .chunk:focus-within .chunk-divider { opacity: 1; }
.chunk-actions .el-button { padding: 6px; }
.chunk-actions .el-button .el-icon { font-size: 16px; }
.chunk-content { overflow-wrap: anywhere; overflow: auto; line-height: 1.7; }
.chunk-content :deep(mark) { background: var(--el-color-warning-light-8, #f3d19e); color: inherit; padding: 0 2px; border-radius: 2px; }
.chunk-content :deep(table) { border-collapse: collapse; width: 100%; }
.chunk-content :deep(td), .chunk-content :deep(th) { border: 1px solid var(--el-border-color-lighter); padding: 6px; }
.chunk-content :deep(img) { max-width: 100%; border-radius: 4px; margin: 6px 0; display: block; }

/* 子分段（父子分段模式）：父卡片内容下方的轻分隔区块；全文父块模式（.main）为卡片主体、无顶部分隔线 */
.child-chunks { margin-top: 10px; border-top: 1px dashed var(--el-border-color-lighter); padding-top: 8px; }
.child-chunks.main { margin-top: 0; border-top: none; padding-top: 0; }
.child-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.child-title { font-size: 13px; font-weight: 600; color: #303133; }
.child-count { font-size: 12px; color: #909399; }
.child-tip { color: #c0c4cc; font-size: 14px; cursor: help; }
/* 子分段开关：浅蓝 chip + 旋转箭头（对齐参考稿「∨ 子分段」样式） */
.child-toggle {
  display: inline-flex; align-items: center; gap: 4px;
  border: none; background: var(--el-color-primary-light-9); color: var(--el-color-primary);
  font-size: 12px; font-weight: 600; line-height: 20px; padding: 1px 8px; border-radius: 4px;
  cursor: pointer;
}
.child-toggle:hover { background: var(--el-color-primary-light-8); }
.child-caret { font-size: 12px; transition: transform .2s ease; }
.child-caret.open { transform: rotate(180deg); }
/* 子分段条目：去卡片化，改为左侧竖向强调条 + 序号徽标的扁平层级（对齐参考稿） */
.child-item { position: relative; display: flex; gap: 8px; align-items: flex-start; padding: 6px 4px 6px 12px; }
.child-item::before { content: ''; position: absolute; left: 0; top: 6px; bottom: 6px; width: 3px; border-radius: 2px; background: var(--el-color-primary-light-5); }
.child-item:hover { background: #f8fafd; }
.child-index { flex: none; min-width: 20px; height: 20px; border-radius: 5px; background: var(--el-fill-color); color: var(--el-text-color-secondary); font-size: 12px; font-weight: 600; display: flex; align-items: center; justify-content: center; margin-top: 2px; /* 与子块首行对齐 */ }
.child-content-wrap { flex: 1; min-width: 0; }
.child-content { flex: 1; min-width: 0; font-size: 13px; line-height: 1.6; color: var(--el-text-color-regular); overflow-wrap: anywhere; }
.child-content :deep(img) { max-width: 100%; border-radius: 4px; }
.child-content :deep(table) { border-collapse: collapse; }
.child-content :deep(td), .child-content :deep(th) { border: 1px solid var(--el-border-color-lighter); padding: 4px; }
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
