<script setup lang="ts">
// 知识中心：跨文档库聚合展示所有来源的知识源文档（手动上传 + 钉钉知识库同步）。
// 元数据来自 DB（LibraryDocument 聚合），原件存于 MinIO（下载原文经文档库接口流式返回）；
// 钉钉来源固化了在线文档链接与知识库名称（迁移 0051 后入库写入，存量由映射反查兜底），
// 知识标题可点击跳转钉钉知识库，来源系统展示「钉钉+知识库名称」。
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import KgPagination from '@/components/common/KgPagination.vue'
import { fetchSourceDocuments, fetchSourceDocumentTags, updateSourceDocument } from '@/api/knowledge-center'
import { downloadOriginal } from '@/api/document-library'
import type { SourceDoc } from '@/types/knowledge-center'
import { fileIcon } from '@/utils/file-icon'
import { formatDate } from '@/utils/format'

// ===== 列表与筛选 =====
const items = ref<SourceDoc[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(20)
const loading = ref(false)
const keyword = ref('')
const sourceFilter = ref('')
const tagFilter = ref('')
const tagOptions = ref<Array<{ name: string; count: number }>>([])
const SOURCE_OPTIONS = [
  { label: '钉钉知识库', value: 'dingtalk' },
  { label: '手动上传', value: 'local' },
]

async function loadTags() {
  try {
    const res = await fetchSourceDocumentTags()
    tagOptions.value = res.items
  } catch { /* 拦截器已提示 */ }
}

async function load() {
  loading.value = true
  try {
    const res = await fetchSourceDocuments({
      page: page.value,
      size: size.value,
      search: keyword.value.trim() || undefined,
      source: sourceFilter.value || undefined,
      tag: tagFilter.value || undefined,
    })
    items.value = res.items
    total.value = res.total
  } catch { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}

function reset() {
  keyword.value = ''
  sourceFilter.value = ''
  tagFilter.value = ''
  page.value = 1
  load()
}

function onPageChange(p: number) {
  page.value = p
  load()
}

function onSizeChange(s: number) {
  page.value = 1
  size.value = s
  load()
}

// ===== 展示 =====
function sourceLabel(row: SourceDoc): string {
  if (row.source === 'dingtalk') {
    return row.source_workspace_name ? `钉钉 · ${row.source_workspace_name}` : '钉钉'
  }
  return '手动上传'
}

function truncatedTags(tags: string[]): string[] {
  return tags.slice(0, 5)
}

function isExpired(expireAt: string | null): boolean {
  if (!expireAt) return false
  return new Date(expireAt).getTime() < Date.now()
}

// ===== 下载原文（PENDING=待同步占位，尚无原件） =====
const downloading = ref<string | null>(null)
async function downloadDoc(row: SourceDoc) {
  if (row.status === 'PENDING') return
  downloading.value = row.id
  try {
    const blob = await downloadOriginal(row.library_id, row.id)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = row.name
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch { /* 拦截器已提示 */ } finally {
    downloading.value = null
  }
}

// ===== 编辑：标签 + 过期时间 =====
const editDialog = ref(false)
const editSaving = ref(false)
const editDoc = ref<SourceDoc | null>(null)
const editTags = ref<string[]>([])
const editExpire = ref<string>('')
const tagInput = ref('')

function openEdit(row: SourceDoc) {
  editDoc.value = row
  editTags.value = [...(row.tags || [])]
  editExpire.value = row.expire_at ? row.expire_at.slice(0, 10) : ''
  tagInput.value = ''
  editDialog.value = true
}

function addTag() {
  const t = tagInput.value.trim()
  if (!t) return
  if (!editTags.value.includes(t)) editTags.value.push(t)
  tagInput.value = ''
}

function removeTag(t: string) {
  editTags.value = editTags.value.filter((x) => x !== t)
}

async function saveEdit() {
  if (!editDoc.value) return
  editSaving.value = true
  try {
    const updated = await updateSourceDocument(editDoc.value.id, {
      tags: editTags.value,
      expire_at: editExpire.value || null,
    })
    const idx = items.value.findIndex((d) => d.id === updated.id)
    if (idx >= 0) items.value[idx] = updated
    ElMessage.success('已保存')
    editDialog.value = false
    loadTags()  // 标签可能变更，刷新筛选选项
  } catch { /* 拦截器已提示 */ } finally {
    editSaving.value = false
  }
}

onMounted(() => {
  load()
  loadTags()
})
</script>

<template>
  <div class="kge-page kc-page">
    <el-card shadow="never" class="kc-card">
      <!-- 筛选栏 -->
      <el-form inline class="filter-bar" @submit.prevent>
        <el-form-item label="来源">
          <el-select v-model="sourceFilter" placeholder="全部来源" clearable style="width: 150px" @change="search">
            <el-option v-for="o in SOURCE_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="标签">
          <el-select v-model="tagFilter" placeholder="全部标签" clearable filterable style="width: 150px" @change="search">
            <el-option v-for="t in tagOptions" :key="t.name" :label="t.name" :value="t.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="知识标题">
          <el-input v-model="keyword" placeholder="按标题搜索" clearable style="width: 220px"
                    :prefix-icon="Search" @keyup.enter="search" @clear="search" />
        </el-form-item>
        <el-form-item class="filter-actions">
          <el-button type="primary" @click="search">查询</el-button>
          <el-button @click="reset">重置</el-button>
        </el-form-item>
      </el-form>

      <!-- 文档表（border 开启表头列宽拖拽） -->
      <el-table :data="items" v-loading="loading" size="small" border row-key="id">
        <el-table-column label="知识标题" min-width="280" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="doc-name">
              <img :src="fileIcon(row.name)" alt="" class="doc-icon" />
              <a v-if="row.source_url" :href="row.source_url" target="_blank" rel="noopener"
                 class="doc-link" :title="`在钉钉知识库中打开：${row.name}`">{{ row.name }}</a>
              <span v-else class="doc-text">{{ row.name }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="标签" min-width="170">
          <template #default="{ row }">
            <template v-if="row.tags?.length">
              <el-tag v-for="t in truncatedTags(row.tags)" :key="t" size="small" effect="plain" type="info">{{ t }}</el-tag>
              <el-tag v-if="row.tags.length > 5" size="small" effect="plain" type="info">…</el-tag>
            </template>
            <span v-else class="cell-none">-</span>
          </template>
        </el-table-column>
        <el-table-column label="来源系统" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tag v-if="row.source === 'dingtalk'" size="small" effect="light" type="primary" class="src-tag">
              {{ sourceLabel(row as SourceDoc) }}
            </el-tag>
            <span v-else class="src-local">手动上传</span>
          </template>
        </el-table-column>
        <el-table-column label="更新人" width="110" show-overflow-tooltip>
          <template #default="{ row }">{{ row.updated_by || '-' }}</template>
        </el-table-column>
        <el-table-column label="更新时间" width="150">
          <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="过期时间" width="150">
          <template #default="{ row }">
            <span :class="{ expired: isExpired(row.expire_at) }">{{ formatDate(row.expire_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row as SourceDoc)">编辑</el-button>
            <el-button link type="primary" size="small" :disabled="row.status === 'PENDING'"
                       :loading="downloading === row.id" @click="downloadDoc(row as SourceDoc)">下载原文</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无知识源文档" :image-size="72">
            <template #extra>
              <span class="empty-hint">可在「知识源管理」接入钉钉知识库，或在「知识库」中上传文档</span>
            </template>
          </el-empty>
        </template>
      </el-table>

      <KgPagination :page="page" :size="size" :total="total" :sizes="[20, 50, 100]"
                    layout="total, sizes, prev, pager, next"
                    @update:page="onPageChange" @update:size="onSizeChange" />
    </el-card>

    <!-- 编辑：标签 + 过期时间 -->
    <el-dialog v-model="editDialog" :title="`编辑 · ${editDoc?.name || ''}`" width="480px" :close-on-click-modal="false">
      <el-form label-width="80px" @submit.prevent>
        <el-form-item label="标签">
          <div class="edit-tags">
            <div v-if="editTags.length" class="tag-list">
              <el-tag v-for="t in editTags" :key="t" closable size="small" effect="plain" @close="removeTag(t)">{{ t }}</el-tag>
            </div>
            <el-input v-model="tagInput" placeholder="输入标签后按回车添加" @keydown.enter.prevent="addTag" />
          </div>
        </el-form-item>
        <el-form-item label="过期时间">
          <el-date-picker v-model="editExpire" type="date" value-format="YYYY-MM-DD"
                          placeholder="选择过期日期（留空表示不过期）" style="width: 100%" clearable />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialog = false">取消</el-button>
        <el-button type="primary" :loading="editSaving" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.kc-page {
  overflow: auto;
}

.kc-card {
  border-radius: 12px;
}

/* 筛选栏：flex 布局 + 查询/重置按钮组靠右（与其他列表页统一） */
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
}

.filter-bar :deep(.el-form-item) {
  margin-bottom: 10px;
}

.filter-bar :deep(.filter-actions) {
  margin-left: auto;
}

.doc-name {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.doc-icon {
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  object-fit: contain;
}

.doc-link {
  color: var(--el-color-primary);
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.doc-link:hover {
  text-decoration: underline;
}

.doc-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cell-none {
  color: var(--el-text-color-placeholder);
}

.src-tag {
  max-width: 100%;
}

.src-local {
  color: var(--el-text-color-regular);
}

.expired {
  color: var(--el-color-danger);
}

.edit-tags {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.empty-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
