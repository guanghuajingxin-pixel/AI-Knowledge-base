<script setup lang="ts">
/**
 * 知识中心 - 钉钉知识（入库审核）
 * 读取服务端持久化快照（重启后仍在）；全量遍历只在点「刷新」时触发，
 * 支持按知识库 / 创建人 / 目录过滤；操作列可对文件做入库审核（通过/待确认/待更正）
 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Search, Refresh } from '@element-plus/icons-vue'
import { fileIcon } from '@/utils/file-icon'
import { ElMessage, ElMessageBox } from 'element-plus'
import { fetchDingTalkDocuments, reviewDingTalkDocument } from '@/api/knowledge-center'
import { formatDate, formatFileSize } from '@/utils/format'
import { useUserStore } from '@/stores/user'
import type { DingTalkFile, DingTalkOption, DingTalkReviewStatus } from '@/types/knowledge-center'

const userStore = useUserStore()

// 数据
const documents = ref<DingTalkFile[]>([])
const total = ref(0)
const loading = ref(false)        // 首次同步（无数据）时的全屏遮罩
const refreshing = ref(false)     // 后台遍历中（手动刷新按钮态）
const page = ref(1)
const size = ref(20)

// 快照信息：最近一次手动刷新的时间（服务端持久化，进程重启后仍可用）
const cachedAt = ref<string | null>(null)
const snapshotTip = '列表为最近一次手动刷新的持久化快照（服务重启后仍在）；点「刷新」重新遍历钉钉知识库更新，全量遍历约 25–30 分钟'
const cachedAtText = computed(() => {
  if (!cachedAt.value) return ''
  const d = new Date(cachedAt.value)
  if (isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
})
const emptyText = computed(() => (cachedAt.value
  ? '没有符合条件的文件'
  : '暂无钉钉知识快照：点右上角「刷新」从钉钉同步（全量遍历约 25–30 分钟）'))

// 后台遍历耗时较长时，按此间隔轮询
const POLL_INTERVAL = 12000
let pollTimer: ReturnType<typeof setTimeout> | null = null

// 过滤条件
const searchKeyword = ref('')
const directoryKeyword = ref('')
const wsFilter = ref<string[]>([])
const creatorFilter = ref<string[]>([])

// 下拉选项（来自后端聚合的完整集合）
const wsOptions = ref<DingTalkOption[]>([])
const creatorOptions = ref<DingTalkOption[]>([])

// 钉钉未配置等错误
const errorMsg = ref('')

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

async function loadDocs(forceRefresh = false, fromPoll = false) {
  if (!fromPoll) {
    errorMsg.value = ''
    if (documents.value.length === 0) loading.value = true
    if (forceRefresh) refreshing.value = true
  }
  try {
    const params: any = {
      page: page.value,
      size: size.value,
      refresh: forceRefresh || undefined,
    }
    if (searchKeyword.value) params.search = searchKeyword.value
    if (directoryKeyword.value) params.directory = directoryKeyword.value
    if (wsFilter.value.length) params.workspace_id = wsFilter.value.join(',')
    if (creatorFilter.value.length) params.creator_id = creatorFilter.value.join(',')

    const res = await fetchDingTalkDocuments(params)
    documents.value = res.items
    total.value = res.total
    wsOptions.value = res.workspaces || []
    creatorOptions.value = res.creators || []
    errorMsg.value = res.error || ''
    cachedAt.value = res.cached_at ?? cachedAt.value

    if (res.loading) {
      // 后台遍历中：无数据时保持遮罩，有数据时保持刷新按钮态，稍后轮询
      loading.value = documents.value.length === 0
      refreshing.value = true
      stopPolling()
      pollTimer = setTimeout(() => loadDocs(false, true), POLL_INTERVAL)
      return
    }

    // 后台遍历完成（失败时 error 已由后端写入 errorMsg，不提示成功）
    stopPolling()
    const wasRefreshing = refreshing.value
    loading.value = false
    refreshing.value = false
    if ((forceRefresh || (wasRefreshing && fromPoll)) && !errorMsg.value) {
      ElMessage.success('钉钉知识快照已刷新')
    }
  } catch (e: any) {
    stopPolling()
    loading.value = false
    refreshing.value = false
    errorMsg.value = e?.message || '钉钉数据拉取失败'
  }
}

function handleSearch() {
  page.value = 1
  loadDocs()
}

function handleReset() {
  searchKeyword.value = ''
  directoryKeyword.value = ''
  wsFilter.value = []
  creatorFilter.value = []
  page.value = 1
  loadDocs()
}

async function handleRefresh() {
  if (refreshing.value) return
  try {
    await ElMessageBox.confirm(
      '将重新遍历钉钉全部知识库（约 4,500 次节点请求，需 25–30 分钟），期间列表继续显示当前快照。是否开始刷新？',
      '刷新钉钉知识快照',
      { type: 'warning', confirmButtonText: '开始刷新', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  page.value = 1
  loadDocs(true)
}

function handlePageChange(p: number) {
  page.value = p
  loadDocs()
}

function handleSizeChange(s: number) {
  size.value = s
  page.value = 1
  loadDocs()
}

// ===== 入库审核：操作列按钮即状态字段（未审核显示「审核」，已审核显示状态文案）=====
// 点击弹窗选择 通过/待确认/待更正，确认后文案变为所选状态，再次点击可变更
const REVIEW_STATUS_TYPES: Record<string, 'success' | 'warning' | 'danger'> = {
  通过: 'success',
  待确认: 'warning',
  待更正: 'danger',
}
const reviewDialog = ref(false)
const reviewRow = ref<DingTalkFile>()
const reviewStatus = ref<DingTalkReviewStatus>('通过')
const reviewSaving = ref(false)

function openReview(row: DingTalkFile) {
  reviewRow.value = row
  reviewStatus.value = (row.review_status || '通过') as DingTalkReviewStatus
  reviewDialog.value = true
}

function reviewBtnType(row: DingTalkFile): 'success' | 'warning' | 'danger' | 'primary' {
  return (row.review_status && REVIEW_STATUS_TYPES[row.review_status]) || 'primary'
}

async function confirmReview() {
  const row = reviewRow.value
  if (!row) return
  reviewSaving.value = true
  try {
    const res = await reviewDingTalkDocument({
      node_id: row.node_id,
      workspace_id: row.workspace_id,
      review_status: reviewStatus.value,
    })
    // 行内即时更新：状态文案 + 审核人（后端记录为操作者用户名）
    row.review_status = res.review_status as DingTalkReviewStatus
    row.reviewer = res.reviewer || userStore.userInfo?.username || ''
    reviewDialog.value = false
    ElMessage.success(`已审核：${row.name} → ${res.review_status}`)
  } catch { /* API interceptor displays errors. */ }
  finally { reviewSaving.value = false }
}

function fileTypeText(row: DingTalkFile): string {
  if (row.extension) return row.extension.toUpperCase()
  if (row.category) return row.category
  return '-'
}

onMounted(() => {
  loadDocs()
})

onUnmounted(() => {
  stopPolling()
})
</script>

<template>
  <div class="dt-manage" v-loading="loading" element-loading-text="正在从钉钉知识库同步数据，请稍候…">
    <!-- 顶部状态栏：同步提示 + 文件总数 + 缓存时间 -->
    <div class="action-bar">
      <div class="action-left">
        <el-alert
          v-if="errorMsg"
          :title="errorMsg"
          type="warning"
          :closable="false"
          show-icon
          class="dt-alert"
        />
      </div>
      <div class="action-right">
        <span class="total-hint">共 {{ total }} 个文件</span>
        <el-tooltip v-if="cachedAtText" :content="snapshotTip" placement="top">
          <span class="cache-hint">数据更新于 {{ cachedAtText }}</span>
        </el-tooltip>
        <span v-if="refreshing" class="sync-hint">后台同步中…</span>
      </div>
    </div>

    <!-- 筛选栏：左侧条件自适应换行，右侧「查询 / 重置 / 刷新」成组固定，行尾对齐 -->
    <div class="filter-bar">
      <div class="filter-fields">
        <el-input
          v-model="searchKeyword"
          class="f-search"
          placeholder="搜索文件名称"
          clearable
          @keyup.enter="handleSearch"
          @clear="handleSearch"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-select
          v-model="wsFilter"
          class="f-ws"
          placeholder="按知识库过滤"
          multiple
          collapse-tags
          collapse-tags-tooltip
          clearable
          @change="handleSearch"
        >
          <el-option
            v-for="ws in wsOptions"
            :key="ws.id"
            :label="ws.name"
            :value="ws.id"
          />
        </el-select>
        <el-select
          v-model="creatorFilter"
          class="f-creator"
          placeholder="按创建人过滤"
          multiple
          collapse-tags
          collapse-tags-tooltip
          clearable
          @change="handleSearch"
        >
          <el-option
            v-for="c in creatorOptions"
            :key="c.id"
            :label="c.name"
            :value="c.id"
          />
        </el-select>
        <el-input
          v-model="directoryKeyword"
          class="f-dir"
          placeholder="上级目录（如 /新人导航）"
          clearable
          @keyup.enter="handleSearch"
          @clear="handleSearch"
        />
      </div>
      <div class="filter-actions">
        <el-button type="primary" @click="handleSearch">查询</el-button>
        <el-button @click="handleReset">重置</el-button>
        <el-button :icon="Refresh" :loading="refreshing" @click="handleRefresh">刷新</el-button>
      </div>
    </div>

    <!-- 表格：撑满剩余高度，表体内部滚动 -->
    <div class="table-fill">
      <el-table :data="documents" style="width: 100%" height="100%" stripe :empty-text="emptyText">
      <el-table-column label="文件名称" min-width="360" prop="name" show-overflow-tooltip>
        <template #default="scope: any">
          <el-link type="primary" :underline="false" :href="scope.row.url" target="_blank" :disabled="!scope.row.url">
            <img class="file-icon" :src="fileIcon(scope.row.name)" alt="" />
            {{ scope.row.name }}
          </el-link>
        </template>
      </el-table-column>
      <el-table-column label="来源知识库" width="160" prop="workspace_name" show-overflow-tooltip>
        <template #default="scope: any">
          {{ scope.row.workspace_name || '-' }}
        </template>
      </el-table-column>
      <el-table-column label="上级目录" min-width="200" prop="directory_path" show-overflow-tooltip>
        <template #default="scope: any">
          <span class="dir-path" :title="scope.row.directory_path">{{ scope.row.directory_path }}</span>
        </template>
      </el-table-column>
      <el-table-column label="文件类型" width="100">
        <template #default="scope: any">
          <el-tag size="small" effect="plain">{{ fileTypeText(scope.row) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="文件大小" width="110">
        <template #default="scope: any">
          <span class="col-muted">{{ scope.row.size ? formatFileSize(scope.row.size) : '-' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="创建人" width="110" prop="creator_name">
        <template #default="scope: any">
          {{ scope.row.creator_name || '-' }}
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="scope: any">
          {{ formatDate(scope.row.created_at) }}
        </template>
      </el-table-column>
      <el-table-column label="最近更新" width="170">
        <template #default="scope: any">
          {{ formatDate(scope.row.modified_at) }}
        </template>
      </el-table-column>
      <el-table-column label="审核人" width="100" prop="reviewer">
        <template #default="scope: any">
          {{ scope.row.reviewer || '-' }}
        </template>
      </el-table-column>
      <!-- 操作列即审核状态字段：未审核显示「审核」，已审核显示状态文案，点击均可弹窗（变更）审核 -->
      <el-table-column label="操作" width="80" fixed="right">
        <template #default="scope: any">
          <el-tooltip :disabled="!scope.row.reviewer"
                      :content="`审核人：${scope.row.reviewer}`"
                      placement="top">
            <el-button link :type="reviewBtnType(scope.row)" size="small" @click="openReview(scope.row)">
              {{ scope.row.review_status || '审核' }}
            </el-button>
          </el-tooltip>
        </template>
      </el-table-column>
    </el-table>
    </div>

    <!-- 分页 -->
    <div class="pagination-wrap">
      <el-pagination
        v-if="total > size"
        v-model:current-page="page"
        v-model:page-size="size"
        :total="total"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        @current-change="handlePageChange"
        @size-change="handleSizeChange"
      />
    </div>

    <!-- 入库审核弹窗：选择审核状态，确认后操作列文案变为所选状态，可再次点击变更 -->
    <el-dialog v-model="reviewDialog" title="入库审核" width="min(420px, 90vw)" :close-on-click-modal="!reviewSaving">
      <p class="review-file" :title="reviewRow?.name">{{ reviewRow?.name }}</p>
      <el-radio-group v-model="reviewStatus" class="review-options">
        <el-radio value="通过">通过</el-radio>
        <el-radio value="待确认">待确认</el-radio>
        <el-radio value="待更正">待更正</el-radio>
      </el-radio-group>
      <template #footer>
        <el-button :disabled="reviewSaving" @click="reviewDialog = false">取消</el-button>
        <el-button type="primary" :loading="reviewSaving" @click="confirmReview">确认</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.dt-manage {
  height: 100%;
  background: #fff;
  padding: 16px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}

.action-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  flex-shrink: 0;
  gap: 12px;
}

.action-left {
  flex: 1;
  min-width: 0;
}

.dt-alert {
  padding: 4px 12px;
}

.action-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.filter-bar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  flex-shrink: 0;
}

/* 筛选条件：占据整行剩余宽度，窄屏时自行换行，不挤压右侧按钮组 */
.filter-fields {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  flex: 1 1 560px;
  min-width: 0;
}

.filter-fields :deep(.el-input),
.filter-fields :deep(.el-select) {
  flex: 0 1 auto;
}

.f-search {
  width: 200px;
}

.f-ws {
  width: 210px;
}

.f-creator {
  width: 170px;
}

.f-dir {
  width: 210px;
}

/* 查询 / 重置 / 刷新：固定成组靠右，行尾对齐 */
.filter-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  margin-left: auto;
}

.filter-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.file-icon {
  width: 20px;
  height: 20px;
  object-fit: contain;
  margin-right: 4px;
  vertical-align: -5px;
}

.dir-path {
  color: #606266;
  font-size: 13px;
}

.table-fill {
  flex: 1;
  min-height: 0;
}

.pagination-wrap {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  margin-top: 12px;
  flex-shrink: 0;
}

.total-hint {
  font-size: 13px;
  color: #909399;
}

.cache-hint {
  font-size: 13px;
  color: #909399;
  cursor: help;
}

.sync-hint {
  font-size: 13px;
  color: var(--el-color-primary);
}

.col-muted {
  color: #909399;
}

/* 入库审核弹窗：文件名单行截断，三个状态选项竖排 */
.review-file {
  margin: 0 0 12px;
  font-weight: 600;
  color: #303133;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.review-options {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}
.review-options .el-radio {
  margin-right: 0;
}
</style>
