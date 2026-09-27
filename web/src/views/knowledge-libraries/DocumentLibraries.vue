<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type UploadFile } from 'element-plus'
import { ArrowDown, Back, Clock, Document, Plus, QuestionFilled, Search, Upload } from '@element-plus/icons-vue'
import { listDocumentLibraries, saveDocumentLibrary, listLibraryDocuments, uploadLibraryDocument,
  libraryDocumentAction, setDocumentEnabled, deleteLibraryDocument, setDocumentConfig,
  deleteDocumentLibrary, exportDocumentLibrary, listEmbeddingModels, downloadOriginal, setLibraryDocumentTags,
  type DocumentLibrary, type LibraryDocument } from '@/api/document-library'
import IndexSettingsDialog from '@/components/library/IndexSettingsDialog.vue'
import IndexSettingsPanel from '@/components/library/IndexSettingsPanel.vue'
import { settingsFromConfig, settingsToConfig, librarySettingsFromConfig, defaultSettings, METHODS, type IndexSettings } from '@/components/library/index-settings'
import { updateKnowledgeLibrary } from '@/api/knowledge-library'
import { useUserStore } from '@/stores/user'
import { formatDateTime, formatFileSize, formatSource } from '@/utils/format'
const router = useRouter()
const props = defineProps<{ openLibId?: number }>()
const canPublish = computed(() => ['admin', 'super_admin'].includes(useUserStore().userInfo?.role || ''))
const libraries = ref<DocumentLibrary[]>([])
const selected = ref<DocumentLibrary>()
const documents = ref<LibraryDocument[]>([])
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const keyword = ref('')
const visibleLibraries = computed(() => libraries.value.filter(l => l.name.includes(keyword.value)))
const visibleDocuments = computed(() => documents.value.filter(d => d.name.includes(keyword.value)))
const dialog = ref(false)
const step = ref(0)

// 标签编辑
const tagDialog = ref(false)
const tagDoc = ref<LibraryDocument | null>(null)
const tagInput = ref('')
const tagInputVisible = ref(false)
const tagInputLoading = ref(false)
function openTagDialog(doc: LibraryDocument) {
  tagDoc.value = doc
  tagInput.value = ''
  tagDialog.value = true
  tagInputVisible.value = true
}
async function confirmTags() {
  if (!tagDoc.value) return
  tagInputLoading.value = true
  try {
    await setLibraryDocumentTags(selected.value!.id, tagDoc.value.id, tagDoc.value.tags || [])
    ElMessage.success('标签已保存')
    tagDialog.value = false
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e.message || '保存失败')
  } finally {
    tagInputLoading.value = false
  }
}
function addTagFromInput() {
  const v = tagInput.value.trim()
  if (!v || !tagDoc.value) return
  const tags = tagDoc.value.tags || []
  if (!tags.includes(v)) {
    tags.push(v)
    tagDoc.value.tags = tags
  }
  tagInput.value = ''
}
function removeTag(tag: string) {
  if (!tagDoc.value) return
  tagDoc.value.tags = (tagDoc.value.tags || []).filter(t => t !== tag)
}
function truncatedTags(tags: string[] | undefined) {
  if (!tags || tags.length === 0) return []
  return tags.slice(0, 5)
}
function hasMoreTags(tags: string[] | undefined) {
  return (tags?.length || 0) > 5
}
const editingId = ref<number>()
// 创建/编辑表单：基础信息 + 向量模型；分段设置由共用组件 IndexSettingsPanel 自持（libSettings 为回填初值）
const form = reactive({name: '', description: '', embedding_model: ''})
const libSettings = ref<IndexSettings>(defaultSettings())
const libPanel = ref<InstanceType<typeof IndexSettingsPanel>>()
// 知识库级解析开关：编辑保存后是否对库内全部文档重新解析（默认关闭，批量操作由用户决定）
const reparseAll = ref(false)
const statusLabels: Record<string, string> = {PARSING: '解析中', COMPLETED: '已完成', FAILED: '失败', CANCELLED: '已停止', UNKNOWN: '状态未知'}
async function run(task: () => Promise<void>) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await task() } catch (e: any) { error.value = e?.response?.data?.detail || e.message || '操作失败，请重试' }
  finally { busy.value = false }
}
async function load() {
  loading.value = true
  try {
    if (selected.value) documents.value = await listLibraryDocuments(selected.value.id)
    else libraries.value = await listDocumentLibraries()
  } catch (e: any) { error.value = e.message || '加载失败' }
  finally { loading.value = false }
}
async function openLibrary(lib: DocumentLibrary) { selected.value = lib; keyword.value = ''; await load() }
const embeddingModels = ref<{id: string; name: string}[]>([])
const modelError = ref('')
async function openCreate(lib?: DocumentLibrary) {
  editingId.value = lib?.id; step.value = 0
  Object.assign(form, {name: lib?.name || '', description: lib?.description || '',
    embedding_model: lib?.config?.processing?.embedding_model || ''})
  // 库级全量配置回填（旧库无 strategy 时由 processing 推导），新对象触发共用面板重新初始化
  libSettings.value = librarySettingsFromConfig(lib?.config || null)
  reparseAll.value = false
  dialog.value = true
  modelError.value = ''
  try { embeddingModels.value = await listEmbeddingModels() }
  catch { modelError.value = '模型列表不可用，可手动填写模型名称' }
}
async function save() {
  if (!form.name.trim()) { ElMessage.warning('请输入知识库名称'); return }
  if (!libPanel.value?.validate()) return
  await run(async () => {
    const cfg = settingsToConfig(libPanel.value!.getSettings())
    const lib = await saveDocumentLibrary({
      ...cfg.processing, embedding_model: form.embedding_model,
      name: form.name, description: form.description,
      strategy: cfg.strategy, enhancements: cfg.enhancements, type_rules: cfg.type_rules,
    }, editingId.value)
    if (selected.value?.id === lib.id) selected.value = lib
    dialog.value = false
    if (editingId.value && reparseAll.value) {
      // 解析开关开启：对库内全部非解析中文档逐个触发重新解析
      const docs = await listLibraryDocuments(lib.id).catch(() => [])
      const targets = docs.filter(d => d.status !== 'PARSING')
      let ok = 0
      for (const d of targets) {
        try { await libraryDocumentAction(lib.id, d.id, 'parse'); ok++ } catch { /* 单个失败不阻断其余文档 */ }
      }
      await load()
      ElMessage.success(targets.length ? `设置已保存，已为 ${ok}/${targets.length} 个文档重新解析` : '设置已保存，库内暂无待解析文档')
      return
    }
    await load(); ElMessage.success(editingId.value ? '设置已保存，已有文档需重新解析才生效' : '知识库已创建')
  })
}
async function upload(file: UploadFile) {
  if (!file.raw || !selected.value) return
  const id = selected.value.id
  await run(async () => { await uploadLibraryDocument(id, file.raw!); await load(); ElMessage.success('已上传，正在自动解析') })
}
async function action(doc: LibraryDocument, action: 'parse' | 'stop' | 'refresh') {
  if (!selected.value) return
  if (action === 'parse' && doc.status !== 'FAILED' && doc.status !== 'CANCELLED') {
    try { await ElMessageBox.confirm('重新解析会清除已有分段及人工修改，并使用知识库当前设置。是否继续？', '重新解析', {type: 'warning'}) } catch { return }
  }
  if (action === 'stop') {
    try { await ElMessageBox.confirm('停止解析会清除本次已生成的分段，之后可以重新解析。是否继续？', '停止解析', {type: 'warning'}) } catch { return }
  }
  await run(async () => { const updated = await libraryDocumentAction(selected.value!.id, doc.id, action); Object.assign(doc, updated) })
}
async function remove(doc: LibraryDocument) {
  try { await ElMessageBox.confirm(`删除「${doc.name}」及其分段？`, '删除文档', {type: 'warning'}) } catch { return }
  await run(async () => { await deleteLibraryDocument(selected.value!.id, doc.id); await load(); ElMessage.success('文档已删除') })
}
function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function exportLibrary() { await run(async () => download(await exportDocumentLibrary(selected.value!.id), `${selected.value!.name}.zip`)) }
async function original(doc: LibraryDocument) { await run(async () => download(await downloadOriginal(selected.value!.id, doc.id), doc.name)) }
function docCommand(cmd: string, doc: LibraryDocument) {
  if (cmd === 'original') original(doc)
  else if (cmd === 'remove') remove(doc)
  else if (cmd === 'parse' || cmd === 'stop') action(doc, cmd)
}
function fmtTime(v?: string | null) {
  if (!v) return '-'
  const d = new Date(v)
  if (isNaN(d.getTime())) return '-'
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
function goSegments(doc: LibraryDocument) {
  if (!selected.value) return
  router.push({name: 'LibraryDocumentSegments', params: {libId: selected.value.id, docId: doc.id}, query: {name: doc.name}})
}
const settingsVisible = ref(false)
const settingsDoc = ref<LibraryDocument>()
const settingsValue = ref<IndexSettings>()
function openSettings(doc: LibraryDocument) {
  settingsDoc.value = doc
  settingsValue.value = settingsFromConfig(doc.config, selected.value?.config)
  settingsVisible.value = true
}
async function saveSettings(v: IndexSettings, reparse: boolean) {
  if (!selected.value || !settingsDoc.value) return
  busy.value = true
  try {
    const updated = await setDocumentConfig(selected.value.id, settingsDoc.value.id, settingsToConfig(v))
    Object.assign(settingsDoc.value, updated)
    settingsVisible.value = false
    if (reparse) {
      // 解析开关开启：立即按新设置重新解析分段
      const parsed = await libraryDocumentAction(selected.value.id, settingsDoc.value.id, 'parse')
      Object.assign(settingsDoc.value, parsed)
      ElMessage.success('设置已保存，正在按新设置重新分段')
    } else {
      ElMessage.success('文档设置已保存，重新解析后生效')
    }
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || e.message || '保存失败') }
  finally { busy.value = false }
}
// 知识详情抽屉
const detailVisible = ref(false)
const detailDoc = ref<LibraryDocument>()
function openDetail(doc: LibraryDocument) {
  detailDoc.value = doc
  detailVisible.value = true
}
const detailSettings = computed(() => settingsFromConfig(detailDoc.value?.config, selected.value?.config))
const strategyLabel = (s: string) => ({auto: '自动', custom: '自定义', parent_child: '父子分段', by_file_type: '按文件类型'} as Record<string, string>)[s] || s
async function toggleDoc(doc: LibraryDocument) {
  if (!selected.value) return
  if (doc.enabled) {
    // 禁用需二次确认；取消则开关保持原状态（:model-value 不自动变更）
    try { await ElMessageBox.confirm('确认禁用吗？禁用后该文档的分段将不再参与检索。', '提示', { type: 'warning' }) } catch { return }
  }
  await run(async () => {
    const updated = await setDocumentEnabled(selected.value!.id, doc.id, !doc.enabled)
    Object.assign(doc, updated)
    ElMessage.success(updated.enabled ? '已启用，分段恢复参与检索' : '已禁用，分段不再参与检索')
  })
}
async function removeLibrary(lib: DocumentLibrary) {
  try { await ElMessageBox.confirm(`删除空文档库「${lib.name}」？请确认已经导出归档。`, '删除空库', {type: 'warning'}) } catch { return }
  await run(async () => { await deleteDocumentLibrary(lib.id); await load(); ElMessage.success('知识库已删除') })
}
async function toggle(lib: DocumentLibrary) {
  await run(async () => { await updateKnowledgeLibrary(lib.id, {enabled: !lib.enabled}); lib.enabled = !lib.enabled })
}
// 检索测试：新页签打开
function goRetrievalTest() {
  if (!selected.value) return
  router.push({ path: `/apply/knowledge-libraries/${selected.value.id}/retrieval-test` })
}
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
async function poll() {
  try {
    if (!busy.value && selected.value) {
      const id = selected.value.id
      for (const doc of documents.value.filter(d => d.status === 'PARSING')) {
        const remote = await libraryDocumentAction(id, doc.id, 'refresh')
        if (selected.value?.id === id) Object.assign(doc, remote)
      }
    }
  } catch { /* Stop polling after a failure; manual refresh remains available. */ return }
  if (!disposed) timer = setTimeout(poll, 5000)
}
onMounted(async () => {
  await load()
  if (props.openLibId) {
    const lib = libraries.value.find(l => l.id === props.openLibId)
    if (lib) await openLibrary(lib)
  }
  timer = setTimeout(poll, 5000)
})
onBeforeUnmount(() => { disposed = true; clearTimeout(timer) })
</script>
<template>
  <div class="document-libraries">
    <el-alert v-if="error" :title="String(error)" type="error" show-icon @close="error = ''" />
    <div class="content" v-loading="loading">
      <div class="toolbar">
        <el-button v-if="selected" link :icon="Back" @click="selected = undefined; keyword = ''; load()">返回</el-button>
        <el-input v-model="keyword" :prefix-icon="Search" :placeholder="selected ? '搜索文档名称' : '搜索知识库名称'" clearable style="max-width: 320px" />
        <template v-if="selected">
          <div class="toolbar-spacer" />
          <el-button :loading="busy" @click="exportLibrary">导出归档</el-button>
          <el-button @click="openCreate(selected)">知识库设置</el-button>
          <el-button v-if="canPublish" :icon="Search" @click="goRetrievalTest">检索测试</el-button>
        </template>
        <el-button v-else class="toolbar-create" type="primary" :icon="Plus" @click="openCreate()">创建知识库</el-button>
        <el-upload v-if="selected" :show-file-list="false" :auto-upload="false" :on-change="upload" :disabled="busy" accept=".pdf,.docx,.txt,.md,.csv,.xlsx,.pptx,.html">
          <el-button type="primary" :icon="Upload" :loading="busy">上传文档</el-button>
        </el-upload>
      </div>
      <template v-if="!selected">
        <el-table :data="visibleLibraries" empty-text="暂无文档库，点击创建知识库开始">
          <el-table-column label="知识库名称" min-width="240" show-overflow-tooltip><template #default="{row}"><el-button link type="primary" @click="openLibrary(row as DocumentLibrary)">{{ row.name }}</el-button></template></el-table-column>
          <el-table-column prop="description" label="描述" min-width="220" show-overflow-tooltip />
          <el-table-column prop="document_count" label="文档数" width="90" />
          <el-table-column label="创建人" width="110" show-overflow-tooltip><template #default="{row}">{{ (row as DocumentLibrary).creator || '-' }}</template></el-table-column>
          <el-table-column label="创建时间" width="150"><template #default="{row}">{{ fmtTime((row as DocumentLibrary).created_at) }}</template></el-table-column>
          <el-table-column label="开放检索" width="140"><template #default="{row}"><div class="switch-line"><el-switch :model-value="row.enabled" :disabled="busy || !canPublish" :aria-label="`${row.name}开放检索`" @change="toggle(row as DocumentLibrary)" /><span>{{ row.enabled ? '已开放' : '已停用' }}</span></div></template></el-table-column>
          <el-table-column label="操作" width="145"><template #default="{row}"><el-button link type="primary" @click="openCreate(row as DocumentLibrary)">设置</el-button><el-tooltip content="先停用并清空文档后可删除空库"><el-button v-if="canPublish" link type="danger" :disabled="busy || row.enabled || row.document_count > 0" @click="removeLibrary(row as DocumentLibrary)">删除</el-button></el-tooltip></template></el-table-column>
        </el-table>
      </template>
      <template v-else>
        <el-table :data="visibleDocuments" empty-text="暂无文档，请上传文件">
          <el-table-column label="文档名称" min-width="260" show-overflow-tooltip><template #default="{row}"><el-button link type="primary" :disabled="row.status === 'PARSING'" @click="goSegments(row as LibraryDocument)">{{ row.name }}</el-button></template></el-table-column>
          <el-table-column label="标签" min-width="220"><template #default="{row}">
            <div class="tag-cell">
              <template v-for="t in truncatedTags(row.tags)" :key="t"><el-tag size="small" effect="plain" type="info">{{ t }}</el-tag></template>
              <el-tag v-if="hasMoreTags(row.tags)" size="small" effect="plain" type="info">…</el-tag>
              <el-button link type="primary" size="small" @click="openTagDialog(row as LibraryDocument)">+ 添加</el-button>
            </div>
          </template></el-table-column>
          <el-table-column label="解析状态" width="155"><template #default="{row}"><el-tag :type="row.status === 'COMPLETED' ? 'success' : row.status === 'FAILED' ? 'danger' : 'info'">{{ statusLabels[row.status] || row.status }}</el-tag><el-progress v-if="row.status === 'PARSING'" :percentage="Math.round(row.progress * 100)" /></template></el-table-column>
          <el-table-column prop="chunk_count" label="分段" width="75" />
          <el-table-column label="更新时间" width="180"><template #default="{row}">{{ formatDateTime(row.updated_at) }}</template></el-table-column>
          <el-table-column label="训练时间" width="180"><template #default="{row}">{{ row.parsed_at ? formatDateTime(row.parsed_at) : '-' }}</template></el-table-column>
          <el-table-column label="来源" width="100"><template #default="{row}"><el-tag size="small" :type="row.source === 'dingtalk' ? 'warning' : 'info'">{{ formatSource(row.source) }}</el-tag></template></el-table-column>
          <el-table-column label="状态" width="110"><template #default="{row}"><div class="switch-line"><el-switch :model-value="row.enabled" :disabled="busy || row.status === 'PARSING'" :aria-label="`${row.name}检索状态`" @change="toggleDoc(row as LibraryDocument)" /><span>{{ row.enabled ? '已启用' : '已禁用' }}</span></div></template></el-table-column>
          <el-table-column label="操作" width="180" fixed="right"><template #default="{row}">
            <div class="ops">
              <el-button link type="primary" @click="openDetail(row as LibraryDocument)">详情</el-button>
              <el-button link type="primary" :disabled="row.status === 'PARSING'" @click="openSettings(row as LibraryDocument)">设置</el-button>
              <el-dropdown trigger="click" @command="(cmd: string) => docCommand(cmd, row as LibraryDocument)">
                <el-button link>更多<el-icon style="margin-left:2px"><ArrowDown /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item v-if="row.status === 'PARSING'" command="stop" :disabled="busy">停止解析</el-dropdown-item>
                    <el-dropdown-item v-else command="parse" :disabled="busy">重新解析</el-dropdown-item>
                    <el-dropdown-item command="original" :disabled="busy">下载原文</el-dropdown-item>
                    <el-dropdown-item command="remove" divided style="color:var(--el-color-danger)" :disabled="busy || row.status === 'PARSING'">删除</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template></el-table-column>
        </el-table>
      </template>
    </div>
    <el-dialog v-model="dialog" class="library-config-dialog" top="5vh" :title="editingId ? '知识库设置' : '创建知识库'" width="min(680px, 94vw)" :close-on-click-modal="false">
      <el-steps :active="step" simple><el-step title="基础信息" /><el-step title="解析与分段" /></el-steps>
      <!-- v-show 保持两步均挂载：共用分段面板在步骤切换间不丢状态 -->
      <div v-show="step === 0">
        <el-form label-width="110px" class="config-form">
          <el-form-item label="知识库名称" required><el-input v-model="form.name" maxlength="200" show-word-limit placeholder="请输入知识库名称" /></el-form-item>
          <el-form-item label="描述"><el-input v-model="form.description" type="textarea" :rows="3" maxlength="2000" /></el-form-item>
        </el-form>
      </div>
      <div v-show="step === 1" class="step-segment">
        <el-alert title="设置作为文档解析的默认规则，不会自动覆盖已有分段；可通过下方开关决定保存后是否重新解析。" type="info" :closable="false" />
        <IndexSettingsPanel ref="libPanel" scope="library" :value="libSettings" />
        <el-form label-width="110px" class="config-form">
          <el-form-item v-if="editingId" label="重新解析">
            <div class="switch-line">
              <el-switch v-model="reparseAll" aria-label="保存后重新解析全部文档" />
              <span>{{ reparseAll ? '保存后重新解析全部文档' : '保存后不重新解析' }}</span>
              <el-tooltip content="重新解析将清除已有分段及人工修改，解析中的文档自动跳过" placement="top"><el-icon class="tip-icon"><QuestionFilled /></el-icon></el-tooltip>
            </div>
          </el-form-item>
          <el-form-item label="解析引擎"><el-tag>MinerU</el-tag><span class="hint">恒做版面识别与 OCR</span></el-form-item>
          <el-form-item label="向量模型"><el-select v-model="form.embedding_model" filterable allow-create clearable placeholder="选择向量模型，留空使用默认"><el-option v-for="m in embeddingModels" :key="m.id" :value="m.id" :label="m.name" /></el-select><span v-if="modelError" class="hint">{{ modelError }}</span><span class="hint">来自「模型配置」中生效的 Embedding 配置；已有分段时切换需重新解析</span></el-form-item>
        </el-form>
      </div>
      <template #footer><el-button @click="dialog = false">取消</el-button><el-button v-if="step" @click="step = 0">上一步</el-button><el-button v-if="!step" type="primary" :disabled="!form.name.trim()" @click="step = 1">下一步</el-button><el-button v-else type="primary" :loading="busy" @click="save">{{ editingId ? '保存设置' : '创建' }}</el-button></template>
    </el-dialog>
    <el-dialog v-model="tagDialog" :title="`编辑标签 · ${tagDoc?.name || ''}`" width="480px" :close-on-click-modal="false">
      <div class="tag-editor">
        <div v-if="tagDoc?.tags?.length" class="tag-list">
          <el-tag v-for="t in tagDoc.tags" :key="t" closable @close="removeTag(t)" size="small" effect="plain">{{ t }}</el-tag>
        </div>
        <div v-else class="hint">暂无标签，下方输入关键词按回车添加</div>
        <el-input
          v-model="tagInput"
          placeholder="输入标签后按回车添加"
          clearable
          size="small"
          @keyup.enter="addTagFromInput"
        />
      </div>
      <template #footer>
        <el-button @click="tagDialog = false">取消</el-button>
        <el-button type="primary" :loading="tagInputLoading" @click="confirmTags">保存</el-button>
      </template>
    </el-dialog>
    <IndexSettingsDialog v-model="settingsVisible" title="文档设置" scope="document" :value="settingsValue || null" @confirm="saveSettings" />
    <el-drawer v-model="detailVisible" title="知识详情" direction="rtl" size="560px">
      <template v-if="detailDoc">
        <div class="detail-section">
          <div class="detail-title">基本信息</div>
          <div class="detail-grid">
            <div class="detail-item span2"><span class="detail-label">知识标题</span><span class="detail-value">{{ detailDoc.name }}</span></div>
            <div class="detail-item"><span class="detail-label">知识ID</span><span class="detail-value">{{ detailDoc.id }}</span></div>
            <div class="detail-item"><span class="detail-label">解析状态</span><el-tag size="small" :type="detailDoc.status === 'COMPLETED' ? 'success' : detailDoc.status === 'FAILED' ? 'danger' : 'info'">{{ statusLabels[detailDoc.status] || detailDoc.status }}</el-tag></div>
            <div class="detail-item"><span class="detail-label">来源系统</span><span class="detail-value">{{ formatSource(detailDoc.source) }}</span></div>
            <div class="detail-item"><span class="detail-label">检索权限</span><el-tag size="small" :type="detailDoc.enabled ? 'success' : 'info'">{{ detailDoc.enabled ? '开放' : '受限' }}</el-tag></div>
            <div class="detail-item"><span class="detail-label">创建时间</span><span class="detail-value">{{ formatDateTime(detailDoc.created_at) }}</span></div>
            <div class="detail-item"><span class="detail-label">更新时间</span><span class="detail-value">{{ formatDateTime(detailDoc.updated_at) }}</span></div>
            <div class="detail-item"><span class="detail-label">训练时间</span><span class="detail-value">{{ detailDoc.parsed_at ? formatDateTime(detailDoc.parsed_at) : '-' }}</span></div>
            <div class="detail-item"><span class="detail-label">分段数量</span><span class="detail-value">{{ detailDoc.chunk_count }}</span></div>
          </div>
        </div>
        <div class="detail-section">
          <div class="detail-title">正文信息</div>
          <div class="detail-file">
            <div class="file-icon"><el-icon :size="18"><Document /></el-icon></div>
            <div class="file-meta">
              <div class="file-name">{{ detailDoc.name }}</div>
              <div class="file-sub">大小：{{ formatFileSize(detailDoc.size) }}<template v-if="detailDoc.parsed_at"><span class="file-sep">|</span><el-icon :size="12"><Clock /></el-icon> 训练于 {{ formatDateTime(detailDoc.parsed_at) }}</template></div>
            </div>
          </div>
        </div>
        <div class="detail-section">
          <div class="detail-title">标签信息</div>
          <div class="detail-grid">
            <div class="detail-item span2"><span class="detail-label">知识标签</span>
              <span class="detail-value">
                <template v-if="detailDoc.tags?.length"><el-tag v-for="t in detailDoc.tags" :key="t" size="small" effect="plain" type="info" style="margin-right:6px">{{ t }}</el-tag></template>
                <template v-else>暂无配置</template>
              </span>
            </div>
          </div>
        </div>
        <div class="detail-section">
          <div class="detail-title">索引设置</div>
          <div class="detail-grid">
            <div class="detail-item"><span class="detail-label">分段策略</span><span class="detail-value">{{ strategyLabel(detailSettings.strategy) }}</span></div>
            <div class="detail-item"><span class="detail-label">分段方式</span><span class="detail-value">{{ METHODS.find(m => m.value === detailSettings.method)?.label || detailSettings.method }}</span></div>
            <div class="detail-item"><span class="detail-label">分段最大长度</span><span class="detail-value">{{ detailSettings.chunk_token_num }}</span></div>
            <div class="detail-item"><span class="detail-label">分段重叠度</span><span class="detail-value">{{ detailSettings.overlap }}</span></div>
            <div class="detail-item span2"><span class="detail-label">文本预处理</span><span class="detail-value">{{ [detailSettings.preprocess.replace_whitespace ? '替换连续空格/换行符/制表符' : '', detailSettings.preprocess.remove_urls_emails ? '删除URL和电子邮箱' : ''].filter(Boolean).join('；') || '未启用' }}</span></div>
          </div>
        </div>
      </template>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
        <el-button type="primary" :disabled="detailDoc?.status === 'PARSING'" @click="detailVisible = false; openSettings(detailDoc!)">编辑</el-button>
      </template>
    </el-drawer>
  </div>
</template>
<style scoped>
.document-libraries { padding: 24px; }
p, .hint { color: #909399; font-size: 13px; }
.content { background: white; padding: 20px; border-radius: 8px; }
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.toolbar-create { margin-left: auto; }
.toolbar-spacer { flex: 1; min-width: 12px; }
.switch-line { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.tip-icon { color: #c0c4cc; font-size: 14px; cursor: help; }
.config-form { margin-top: 24px; }
.step-segment :deep(.index-settings) { margin-top: 16px; }
.step-segment .config-form { margin-top: 16px; }
:global(.library-config-dialog .el-dialog__body) { max-height: calc(85vh - 120px); overflow-y: auto; }
.el-alert { margin-bottom: 16px; }
.hint { margin-left: 8px; }
.chunk { border: 1px solid #ebeef5; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-family: inherit; line-height: 1.7; }
.ops { display: flex; align-items: center; gap: 12px; }
.ops .el-button + .el-button { margin-left: 0; }
.ops .el-dropdown { vertical-align: middle; }
.tag-cell { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.tag-editor .tag-list { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; min-height: 28px; }
.tag-editor .hint { margin-left: 0; margin-bottom: 12px; }
.detail-section { margin-bottom: 24px; }
.detail-title { font-size: 14px; font-weight: 600; color: #303133; padding-left: 8px; border-left: 3px solid var(--el-color-primary); margin-bottom: 14px; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; column-gap: 24px; row-gap: 12px; }
.detail-item { display: flex; align-items: baseline; gap: 12px; min-width: 0; }
.detail-item.span2 { grid-column: 1 / -1; }
.detail-label { flex: none; width: 72px; text-align: right; color: #909399; font-size: 13px; }
.detail-value { color: #303133; font-size: 13px; word-break: break-all; }
.detail-file { display: flex; align-items: center; gap: 12px; background: #f5f7fa; border-radius: 8px; padding: 12px; }
.file-icon { flex: none; width: 36px; height: 36px; border-radius: 6px; background: var(--el-color-danger-light-9, #fef0f0); color: var(--el-color-danger); display: flex; align-items: center; justify-content: center; }
.file-meta { min-width: 0; }
.file-name { font-size: 13px; font-weight: 500; color: #303133; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-sub { display: flex; align-items: center; gap: 4px; margin-top: 4px; color: #909399; font-size: 12px; }
.file-sep { margin: 0 6px; color: #dcdfe6; }
</style>
