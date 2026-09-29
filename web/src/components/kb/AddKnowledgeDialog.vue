<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { UploadFilled, Document, Notebook, Connection } from '@element-plus/icons-vue'
import type { UploadProps, UploadUserFile } from 'element-plus'
import { uploadDocument } from '@/api/document'
import {
  importDingTalk, stageLibraryDocument, discardStagedDocuments, commitStagedDocuments,
  type StagedFile,
} from '@/api/document-library'
import { createSource } from '@/api/sync'
import DingTalkDocTreeDialog from './DingTalkDocTreeDialog.vue'
import DingTalkKbTreeDialog from './DingTalkKbTreeDialog.vue'

type DataSource = 'dingtalk' | 'local'
type ImportMode = 'once' | 'sync'

interface DocSelection {
  workspaceId: string
  workspaceName: string
  rootNodeId: string
  nodes: Array<{ node_id: string; name: string; workspace_id: string }>
}
interface KbSelection {
  workspaces: Array<{ id: string; name: string; root_node_id: string }>
}

const props = defineProps<{
  modelValue: boolean
  /** kb-api 知识库 ID（与 libraryId 二选一） */
  kbId?: string
  /** document-library 知识库 ID（与 kbId 二选一） */
  libraryId?: number
  /** document-library 名称（自动同步创建同步源时用） */
  libraryName?: string
  directoryId?: string | null
}>()
const emit = defineEmits<{
  (e: 'update:modelValue', val: boolean): void
  (e: 'success'): void
}>()

const activeSource = ref<DataSource>('dingtalk')
const importMode = ref<ImportMode>('sync')

// 钉钉文档选择结果
const docSelection = ref<DocSelection | null>(null)
// 钉钉知识库选择结果
const kbSelection = ref<KbSelection | null>(null)

// 子弹窗
const docTreeVisible = ref(false)
const kbTreeVisible = ref(false)

// 本地上传
const fileList = ref<UploadUserFile[]>([])
// 并发暂存上传计数（>0 时确定按钮 loading，避免漏提交仍在路上的文件）
const uploadingCount = ref(0)
const uploading = computed(() => uploadingCount.value > 0)
// 已暂存待确认的文件：el-upload uid → 暂存信息（文档库路径两段式上传）
const stagedItems = ref(new Map<number, StagedFile>())

const selectedCount = computed(() => {
  if (activeSource.value === 'dingtalk') {
    if (docSelection.value) return docSelection.value.nodes.length
    if (kbSelection.value) return kbSelection.value.workspaces.length
    return 0
  }
  // 文档库：已暂存待确认的文件数；旧版知识库：已选文件数
  return props.libraryId != null ? stagedItems.value.size : fileList.value.length
})

const canConfirm = computed(() => selectedCount.value > 0)

function switchSource(source: DataSource) {
  activeSource.value = source
}

function openDocTree() {
  docTreeVisible.value = true
}
function openKbTree() {
  kbTreeVisible.value = true
}

function onDocSelected(sel: DocSelection) {
  docSelection.value = sel
  kbSelection.value = null
}
function onKbSelected(sel: KbSelection) {
  kbSelection.value = sel
  docSelection.value = null
}

const handleUploadChange: UploadProps['onChange'] = async (file) => {
  if (file.status !== 'ready') return
  // 旧版文档知识库（kbId）：维持选文件即上传入库
  if (props.libraryId == null) {
    if (!props.kbId) return
    uploadingCount.value++
    try {
      await uploadDocument(file.raw as File, props.kbId, props.directoryId || undefined)
      ElMessage.success(`${file.name} 上传成功，正在后台处理`)
      emit('success')
    } catch {
      ElMessage.error(`${file.name} 上传失败`)
    } finally {
      uploadingCount.value--
    }
    return
  }
  // 文档库：先暂存（不建文档、不解析），点确定才入库并解析
  uploadingCount.value++
  file.status = 'uploading'
  try {
    const staged = await stageLibraryDocument(props.libraryId, file.raw as File)
    // 弹窗已关闭（取消后上传才完成）：直接丢弃，避免暂存区遗留
    if (!props.modelValue) {
      discardStagedDocuments(props.libraryId, [staged.staging_id]).catch(() => {})
      fileList.value = fileList.value.filter((f) => f.uid !== file.uid)
      return
    }
    // 上传期间用户已把该文件从列表移除：丢弃暂存对象，不进入待确认集合
    if (!fileList.value.some((f) => f.uid === file.uid)) {
      discardStagedDocuments(props.libraryId, [staged.staging_id]).catch(() => {})
      return
    }
    stagedItems.value.set(file.uid, staged)
    file.status = 'success'
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    ElMessage.error(detail ? `${file.name} 上传失败：${detail}` : `${file.name} 上传失败`)
    fileList.value = fileList.value.filter((f) => f.uid !== file.uid)
  } finally {
    uploadingCount.value--
  }
}

/** 移除待确认文件：同步丢弃其 MinIO 暂存对象 */
const handleUploadRemove: UploadProps['onRemove'] = (file) => {
  const staged = stagedItems.value.get(file.uid)
  stagedItems.value.delete(file.uid)
  if (staged && props.libraryId != null) {
    discardStagedDocuments(props.libraryId, [staged.staging_id]).catch(() => {})
  }
}

const confirming = ref(false)

async function handleConfirm() {
  if (activeSource.value === 'local') {
    // 旧版文档知识库：选文件时已上传入库，直接关闭
    if (props.libraryId == null) {
      closeDialog()
      return
    }
    const items = [...stagedItems.value.values()]
    if (items.length === 0) return
    confirming.value = true
    try {
      const res = await commitStagedDocuments(props.libraryId, items)
      stagedItems.value.clear()
      if (res.errors.length > 0) {
        if (res.documents.length > 0) {
          ElMessage.warning(`已添加 ${res.documents.length} 篇文档，${res.errors.length} 篇失败：${res.errors[0]}`)
        } else {
          ElMessage.error(`添加失败：${res.errors[0]}`)
        }
      } else {
        ElMessage.success(`已添加 ${res.documents.length} 篇文档，正在后台解析`)
      }
      if (res.documents.length > 0) emit('success')
      closeDialog()
    } catch (e: any) {
      ElMessage.error(e?.response?.data?.detail || '添加失败，请重试')
    } finally {
      confirming.value = false
    }
    return
  }
  // 钉钉来源需要 libraryId（文档库）才能导入
  if (props.libraryId == null) {
    ElMessage.warning('当前知识库暂不支持钉钉导入，请在文档库中使用')
    return
  }
  confirming.value = true
  try {
    if (importMode.value === 'once') {
      await handleOnceImport()
    } else {
      await handleSyncImport()
    }
  } finally {
    confirming.value = false
  }
}

/** 单次导入：直接调 import-dingtalk 下载并解析 */
async function handleOnceImport() {
  const libId = props.libraryId!
  if (docSelection.value) {
    const nodeIds = docSelection.value.nodes.map((n) => n.node_id)
    const res = await importDingTalk(libId, { node_ids: nodeIds, import_mode: 'once' })
    reportImportResult(res)
  } else if (kbSelection.value) {
    let imported = 0, failed = 0
    const errors: string[] = []
    for (const ws of kbSelection.value.workspaces) {
      const res = await importDingTalk(libId, {
        workspace_id: ws.id, root_node_id: ws.root_node_id, import_mode: 'once',
      })
      imported += res.imported
      failed += res.failed
      errors.push(...res.errors)
    }
    reportImportResult({ imported, failed, errors, documents: [] })
  }
  emit('success')
  closeDialog()
}

/** 自动同步：创建 library 同步源并立即触发一次同步 */
async function handleSyncImport() {
  const libId = props.libraryId!
  const libName = props.libraryName || `文档库 ${libId}`
  if (docSelection.value) {
    const sel = docSelection.value
    await createSource({
      name: `钉钉文档 · ${sel.workspaceName}`,
      workspace_id: sel.workspaceId,
      root_node_id: sel.rootNodeId,
      backend_type: 'library',
      dify_dataset_name: libName,
      dify_dataset_id: String(libId),
      delete_policy: 'keep',
      cron: '*/5 * * * *',
      enabled: true,
      pipeline_inputs: {},
      node_whitelist: sel.nodes.map((n) => n.node_id),
    })
    // 后端创建源时已直跑首次同步，无需前端二次触发
    ElMessage.success(`已创建自动同步任务（${sel.nodes.length} 篇文档，每 5 分钟同步）`)
  } else if (kbSelection.value) {
    for (const ws of kbSelection.value.workspaces) {
      await createSource({
        name: `钉钉知识库 · ${ws.name}`,
        workspace_id: ws.id,
        root_node_id: ws.root_node_id,
        backend_type: 'library',
        dify_dataset_name: libName,
        dify_dataset_id: String(libId),
        delete_policy: 'keep',
        cron: '*/5 * * * *',
        enabled: true,
        pipeline_inputs: {},
        node_whitelist: [],
      })
      // 后端创建源时已直跑首次同步，无需前端二次触发
    }
    ElMessage.success(`已创建 ${kbSelection.value.workspaces.length} 个自动同步任务（每 5 分钟同步）`)
  }
  emit('success')
  closeDialog()
}

function reportImportResult(res: { imported: number; failed: number; errors: string[]; documents?: unknown[] }) {
  if (res.failed === 0) {
    ElMessage.success(`成功导入 ${res.imported} 篇文档，正在后台解析`)
  } else if (res.imported === 0) {
    ElMessage.error(`导入失败：${res.errors[0] || '未知错误'}`)
  } else {
    ElMessage.warning(`导入 ${res.imported} 篇成功，${res.failed} 篇失败`)
  }
}

function closeDialog() {
  docSelection.value = null
  kbSelection.value = null
  fileList.value = []
  emit('update:modelValue', false)
}

async function handleClosed() {
  // 关闭即放弃：未确认的暂存文件从 MinIO 清理（取消/X/ESC/遮罩关闭均触发 closed）
  const staged = [...stagedItems.value.values()]
  stagedItems.value.clear()
  if (props.libraryId != null && staged.length > 0) {
    try {
      await discardStagedDocuments(props.libraryId, staged.map((item) => item.staging_id))
    } catch {
      // 清理失败静默：暂存区有独立前缀，不污染正式数据
    }
  }
  docSelection.value = null
  kbSelection.value = null
  fileList.value = []
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="添加知识"
    width="720px"
    class="add-knowledge-dialog"
    @update:model-value="emit('update:modelValue', $event)"
    @closed="handleClosed"
  >
    <div class="dialog-body">
      <!-- 左侧：选择数据源 -->
      <aside class="source-sidebar">
        <div class="sidebar-title">选择数据源</div>
        <div
          class="source-item"
          :class="{ active: activeSource === 'dingtalk' }"
          @click="switchSource('dingtalk')"
        >
          <el-icon class="source-icon"><Connection /></el-icon>
          <span>钉钉文件</span>
        </div>
        <div
          class="source-item"
          :class="{ active: activeSource === 'local' }"
          @click="switchSource('local')"
        >
          <el-icon class="source-icon"><UploadFilled /></el-icon>
          <span>本地文件</span>
        </div>
      </aside>

      <!-- 右侧：内容区 -->
      <section class="source-content">
        <!-- 钉钉文件 -->
        <template v-if="activeSource === 'dingtalk'">
          <div class="dt-section">
            <div class="section-title">钉钉文件</div>
            <div class="dt-cards">
              <div class="dt-card" @click="openDocTree">
                <div class="dt-card-icon doc">
                  <el-icon><Document /></el-icon>
                </div>
                <div class="dt-card-body">
                  <div class="dt-card-title">钉钉文档</div>
                  <div class="dt-card-desc">添加钉钉文档、钉钉表格、AI表格等</div>
                  <div v-if="docSelection" class="dt-card-selected">
                    已选 {{ docSelection.nodes.length }} 篇 · {{ docSelection.workspaceName }}
                  </div>
                </div>
              </div>
              <div class="dt-card" @click="openKbTree">
                <div class="dt-card-icon kb">
                  <el-icon><Notebook /></el-icon>
                </div>
                <div class="dt-card-body">
                  <div class="dt-card-title">钉钉知识库</div>
                  <div class="dt-card-desc">添加单个知识库，批量学习，随知识库更新自动学习</div>
                  <div v-if="kbSelection" class="dt-card-selected">
                    已选 {{ kbSelection.workspaces.length }} 个知识库
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="dt-section">
            <div class="section-title">导入方式</div>
            <el-radio-group v-model="importMode" class="import-mode-group">
              <label class="import-option" :class="{ checked: importMode === 'once' }">
                <el-radio value="once">单次导入</el-radio>
                <p class="import-desc">直接导入源文件，内容与权限不随源文件更新</p>
              </label>
              <label class="import-option" :class="{ checked: importMode === 'sync' }">
                <el-radio value="sync">自动同步</el-radio>
                <p class="import-desc">实时连接源文件，内容与权限始终与源文件保持同步</p>
              </label>
            </el-radio-group>
          </div>
        </template>

        <!-- 本地文件 -->
        <template v-else>
          <div class="section-title">上传文件</div>
          <el-upload
            v-model:file-list="fileList"
            drag
            multiple
            :auto-upload="false"
            :show-file-list="true"
            :on-change="handleUploadChange"
            :on-remove="handleUploadRemove"
            accept=".docx,.doc,.txt,.xlsx,.xls,.pptx,.ppt,.pdf,.png,.jpg,.wps,.md"
            class="local-upload"
          >
            <div class="upload-icons">
              <span class="file-type-icon pdf">A</span>
              <span class="file-type-icon word">W</span>
              <span class="file-type-icon excel">X</span>
              <span class="file-type-icon ppt">P</span>
            </div>
            <div class="upload-text">点击上传或者拖拽文件到这里</div>
            <div class="upload-formats">
              支持格式：docx, doc, txt, xlsx, xls, pptx, ppt, pdf, png, jpg, wps, md
            </div>
            <div class="upload-limits">
              每个文件不超过 150MB · 可同时批量上传 20 个文件
            </div>
          </el-upload>
        </template>
      </section>
    </div>

    <template #footer>
      <el-button @click="closeDialog">取消</el-button>
      <el-button type="primary" :disabled="!canConfirm" :loading="uploading || confirming" @click="handleConfirm">
        确定
      </el-button>
    </template>

    <DingTalkDocTreeDialog
      v-model="docTreeVisible"
      @selected="onDocSelected"
    />
    <DingTalkKbTreeDialog
      v-model="kbTreeVisible"
      @selected="onKbSelected"
    />
  </el-dialog>
</template>

<style scoped>
.dialog-body {
  display: flex;
  gap: 20px;
  min-height: 360px;
}

/* 左侧数据源 */
.source-sidebar {
  width: 160px;
  flex-shrink: 0;
  border-right: 1px solid #ebeef5;
  padding-right: 20px;
}

.sidebar-title {
  font-size: 14px;
  color: #909399;
  margin-bottom: 12px;
}

.source-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  color: #303133;
  transition: all 0.15s;
  margin-bottom: 4px;
}

.source-item:hover {
  background: #f5f7fa;
}

.source-item.active {
  background: #ecf5ff;
  color: #409eff;
  font-weight: 600;
}

.source-icon {
  font-size: 16px;
}

/* 右侧内容 */
.source-content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.section-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 12px;
}

.dt-section {
  margin-bottom: 24px;
}

.dt-section:last-child {
  margin-bottom: 0;
}

/* 钉钉文件卡片 */
.dt-cards {
  display: flex;
  gap: 16px;
}

.dt-card {
  flex: 1;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 20px 16px;
  cursor: pointer;
  transition: all 0.2s;
  display: flex;
  gap: 12px;
  align-items: flex-start;
}

.dt-card:hover {
  border-color: #409eff;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.12);
}

.dt-card-icon {
  width: 40px;
  height: 40px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  font-size: 20px;
}

.dt-card-icon.doc {
  background: #ecf5ff;
  color: #409eff;
}

.dt-card-icon.kb {
  background: #f0f9eb;
  color: #67c23a;
}

.dt-card-body {
  flex: 1;
  min-width: 0;
}

.dt-card-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 4px;
}

.dt-card-desc {
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
}

.dt-card-selected {
  margin-top: 8px;
  font-size: 12px;
  color: #409eff;
  font-weight: 600;
}

/* 导入方式 */
.import-mode-group {
  display: flex;
  flex-direction: column;
  /* 覆盖 el-radio-group 默认的 align-items:center，两张选项卡等宽、左对齐 */
  align-items: stretch;
  gap: 12px;
  width: 100%;
}

.import-option {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;
}

.import-option:hover {
  border-color: #c0c4cc;
}

.import-option.checked {
  border-color: #409eff;
  background: #ecf5ff;
}

.import-option :deep(.el-radio) {
  margin-right: 0;
  height: auto;
}

.import-desc {
  font-size: 12px;
  color: #909399;
  margin: 2px 0 0 24px;
  line-height: 1.5;
}

/* 本地上传 */
.local-upload {
  width: 100%;
}

.local-upload :deep(.el-upload-dragger) {
  padding: 32px 20px;
  background: #f5f7fa;
  border-color: #dcdfe6;
}

.upload-icons {
  display: flex;
  justify-content: center;
  gap: -4px;
  margin-bottom: 12px;
}

.file-type-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 44px;
  border-radius: 4px;
  font-size: 16px;
  font-weight: 700;
  color: #fff;
  position: relative;
  margin-left: -8px;
}

.file-type-icon:first-child {
  margin-left: 0;
}

.file-type-icon.pdf {
  background: #f56c6c;
}
.file-type-icon.word {
  background: #409eff;
}
.file-type-icon.excel {
  background: #67c23a;
}
.file-type-icon.ppt {
  background: #e6a23c;
}

.upload-text {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 8px;
}

.upload-formats {
  font-size: 12px;
  color: #909399;
  margin-bottom: 4px;
}

.upload-limits {
  font-size: 12px;
  color: #c0c4cc;
}
</style>
