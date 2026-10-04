<script setup lang="ts">
// 同步队列：知识同步过程的逐文档任务列表。
// 三个页签（待处理/处理中/已完成）+ 筛选（同步源/文档类型/执行结果/处理时长/名称/时间）
// + 批量管理（批量重试/批量清理）+ 失败任务级重试（只重处理单个钉钉节点，不整源重跑）。
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, ArrowUp, Collection as CollectionIcon, Refresh } from '@element-plus/icons-vue'
import { batchDeleteTasks, batchRetryTasks, listSources, listTasks, retryTask } from '@/api/sync'
import type { Source, SyncTask } from '@/types/sync'
import { formatDate, formatFileSize } from '@/utils/format'

const router = useRouter()

// ===== 页签与列表 =====
const activeTab = ref<'pending' | 'running' | 'done'>('done')
const tabs = ref({ pending: 0, running: 0, done: 0 })
const items = ref<SyncTask[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(20)
const loading = ref(false)
const selection = ref<SyncTask[]>([])
const sources = ref<Source[]>([])

// ===== 筛选 =====
const filters = ref<{ source_id?: number; ext: string; status: string; duration: string }>({
  source_id: undefined, ext: '', status: '', duration: '',
})
const advanced = ref<{ keyword: string; range: [string, string] | null }>({ keyword: '', range: null })
const expanded = ref(false)

const EXT_OPTIONS = ['docx', 'doc', 'xlsx', 'xls', 'pptx', 'ppt', 'pdf', 'md', 'txt', 'csv', 'adoc', 'axls', 'able']
const DURATION_OPTIONS = [
  { label: '10 秒内', value: 'lt10' },
  { label: '10 秒 ~ 1 分钟', value: '10_60' },
  { label: '1 ~ 5 分钟', value: '60_300' },
  { label: '5 分钟以上', value: 'gt300' },
]

const STATUS_TEXT: Record<string, string> = { pending: '待处理', running: '处理中', success: '成功', failed: '失败' }
const STATUS_TAG: Record<string, 'success' | 'danger' | 'warning' | 'info'> = {
  pending: 'info', running: 'warning', success: 'success', failed: 'danger',
}
const ACTION_TEXT: Record<string, string> = { create: '新建', update: '更新', delete: '删除' }

function formatDuration(sec: number | null | undefined, status: string): string {
  if (status === 'running') return '处理中…'
  if (status === 'pending' || sec == null) return '-'
  if (sec < 60) return `${sec} 秒`
  const m = Math.floor(sec / 60)
  return `${m} 分 ${sec % 60} 秒`
}

// ===== 加载 =====
async function load() {
  loading.value = true
  try {
    const res = await listTasks({
      tab: activeTab.value,
      status: filters.value.status,
      source_id: filters.value.source_id,
      ext: filters.value.ext,
      duration: filters.value.duration,
      keyword: advanced.value.keyword,
      start_from: advanced.value.range?.[0] || '',
      start_to: advanced.value.range?.[1] || '',
      page: page.value,
      size: size.value,
    })
    items.value = res.items
    total.value = res.total
    tabs.value = res.tabs
  } catch { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}

function reset() {
  filters.value = { source_id: undefined, ext: '', status: '', duration: '' }
  advanced.value = { keyword: '', range: null }
  page.value = 1
  load()
}

function onTabChange() {
  selection.value = []
  page.value = 1
  load()
}

function onSizeChange() {
  page.value = 1
  load()
}

function goLibrary() {
  router.push('/apply/knowledge-libraries')
}

// ===== 重试 / 批量 =====
async function retryOne(row: SyncTask) {
  try {
    const res = await retryTask(row.id) as { message?: string }
    ElMessage.success(res?.message || '重试已提交')
    load()
  } catch { /* 拦截器已提示 */ }
}

function retryableIds(): number[] {
  return selection.value.filter((t) => t.status === 'failed').map((t) => t.id)
}

function deletableIds(): number[] {
  return selection.value
    .filter((t) => t.status === 'success' || t.status === 'failed')
    .map((t) => t.id)
}

async function batchRetry() {
  const ids = retryableIds()
  if (!ids.length) {
    ElMessage.warning('请先勾选失败任务')
    return
  }
  try {
    const res = await batchRetryTasks(ids)
    ElMessage.success(res.message)
    selection.value = []
    load()
  } catch { /* 拦截器已提示 */ }
}

async function batchDelete() {
  const ids = deletableIds()
  if (!ids.length) {
    ElMessage.warning('仅已结束（成功/失败）的任务可清理')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将删除 ${ids.length} 条已结束的任务记录（不影响已同步的知识），是否继续？`,
      '批量清理', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const res = await batchDeleteTasks(ids)
    ElMessage.success(res.message)
    selection.value = []
    load()
  } catch { /* 拦截器已提示 */ }
}

function onBatchCommand(command: string) {
  if (command === 'retry') batchRetry()
  else if (command === 'delete') batchDelete()
}

// ===== 轮询：仅当队列中存在待处理/处理中任务时刷新，且不打断勾选操作 =====
let pollTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false
function schedulePoll() {
  pollTimer = setTimeout(async () => {
    if (disposed) return
    if ((tabs.value.pending + tabs.value.running > 0) && !selection.value.length && !loading.value) {
      await load()
    }
    schedulePoll()
  }, 5000)
}

onMounted(async () => {
  listSources().then((list) => { sources.value = list }).catch(() => { /* 拦截器已提示 */ })
  await load()
  // 首次进入：有排队中的任务（如新编目的自动同步）默认停在「待处理」页签
  if (tabs.value.pending > 0 && activeTab.value === 'done') {
    activeTab.value = 'pending'
    page.value = 1
    await load()
  }
  schedulePoll()
})
onBeforeUnmount(() => {
  disposed = true
  clearTimeout(pollTimer)
})
</script>

<template>
  <div class="kge-page queue-page">
    <el-card shadow="never" class="queue-card">
      <!-- 页签 -->
      <el-tabs v-model="activeTab" class="queue-tabs" @tab-change="onTabChange">
        <el-tab-pane name="pending">
          <template #label>
            待处理<span v-if="tabs.pending" class="tab-count">（{{ tabs.pending }}）</span>
          </template>
        </el-tab-pane>
        <el-tab-pane name="running">
          <template #label>
            处理中<span v-if="tabs.running" class="tab-count">（{{ tabs.running }}）</span>
          </template>
        </el-tab-pane>
        <el-tab-pane name="done">
          <template #label>
            已完成<span v-if="tabs.done" class="tab-count">（{{ tabs.done }}）</span>
          </template>
        </el-tab-pane>
      </el-tabs>

      <!-- 筛选栏 -->
      <el-form inline class="filter-bar" @submit.prevent>
        <el-form-item label="同步源">
          <el-select v-model="filters.source_id" placeholder="全部同步源" clearable style="width: 170px">
            <el-option v-for="s in sources" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="文档类型">
          <el-select v-model="filters.ext" placeholder="全部类型" clearable style="width: 130px">
            <el-option v-for="e in EXT_OPTIONS" :key="e" :label="`.${e}`" :value="e" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="activeTab === 'done'" label="执行结果">
          <el-select v-model="filters.status" placeholder="全部结果" clearable style="width: 130px">
            <el-option label="成功" value="success" />
            <el-option label="失败" value="failed" />
          </el-select>
        </el-form-item>
        <template v-if="expanded">
          <el-form-item label="处理时长">
            <el-select v-model="filters.duration" placeholder="全部时长" clearable style="width: 150px">
              <el-option v-for="d in DURATION_OPTIONS" :key="d.value" :label="d.label" :value="d.value" />
            </el-select>
          </el-form-item>
          <el-form-item label="任务名称">
            <el-input v-model="advanced.keyword" placeholder="按文档名称搜索" clearable style="width: 180px"
                      @keyup.enter="search" />
          </el-form-item>
          <el-form-item label="开始时间">
            <el-date-picker v-model="advanced.range" type="daterange" value-format="YYYY-MM-DD"
                            start-placeholder="开始日期" end-placeholder="结束日期" style="width: 240px" />
          </el-form-item>
        </template>
        <el-form-item class="filter-actions">
          <el-button type="primary" @click="search">查询</el-button>
          <el-button @click="reset">重置</el-button>
        </el-form-item>
      </el-form>
      <div class="expand-toggle" @click="expanded = !expanded">
        <span>{{ expanded ? '收起' : '展开' }}</span>
        <el-icon :size="12"><ArrowUp v-if="expanded" /><ArrowDown v-else /></el-icon>
      </div>

      <!-- 批量管理 -->
      <div class="batch-bar">
        <el-dropdown :disabled="!selection.length" @command="onBatchCommand">
          <el-button :disabled="!selection.length">
            批量管理<el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="retry">批量重试（仅失败）</el-dropdown-item>
              <el-dropdown-item command="delete" divided>批量清理（仅已结束）</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <span v-if="selection.length" class="batch-hint">已选 {{ selection.length }} 个任务</span>
        <el-button link type="primary" class="refresh-btn" :icon="Refresh" @click="load">刷新</el-button>
      </div>

      <!-- 任务表 -->
      <el-table :data="items" v-loading="loading" border size="small" row-key="id"
                @selection-change="(rows: unknown[]) => (selection = rows as SyncTask[])">
        <el-table-column type="selection" width="42" reserve-selection />
        <el-table-column label="任务名称" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tag size="small" :type="row.action === 'delete' ? 'warning' : 'info'" class="action-tag">
              {{ ACTION_TEXT[row.action] || row.action }}
            </el-tag>
            <span class="task-name">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="文档类型" width="88">
          <template #default="{ row }">{{ row.file_ext ? `.${row.file_ext}` : '-' }}</template>
        </el-table-column>
        <el-table-column label="文件大小" width="90">
          <template #default="{ row }">{{ formatFileSize(row.file_size) }}</template>
        </el-table-column>
        <el-table-column label="关联知识" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <el-link v-if="row.dataset_name" type="primary" :underline="false" @click="goLibrary">
              <el-icon style="vertical-align: -2px"><CollectionIcon /></el-icon>
              {{ row.dataset_name }}
            </el-link>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="同步源" min-width="120" show-overflow-tooltip>
          <template #default="{ row }">{{ row.source_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="触发方式" width="88">
          <template #default="{ row }">
            <el-tag size="small" :type="row.trigger === 'manual' ? 'warning' : 'info'" effect="plain">
              {{ row.trigger === 'manual' ? '手动' : '定时' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="执行结果" width="100">
          <template #default="{ row }">
            <el-tooltip v-if="row.status === 'failed'" placement="top" :show-after="200">
              <template #content>
                <div style="max-width: 360px">
                  {{ row.error || '未知错误' }}
                  <div v-if="row.retry_count">已重试 {{ row.retry_count }} 次</div>
                </div>
              </template>
              <el-tag size="small" :type="STATUS_TAG[row.status]">{{ STATUS_TEXT[row.status] }}</el-tag>
            </el-tooltip>
            <el-tag v-else size="small" :type="STATUS_TAG[row.status]">{{ STATUS_TEXT[row.status] }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="处理时长" width="96">
          <template #default="{ row }">{{ formatDuration(row.duration_sec, row.status) }}</template>
        </el-table-column>
        <el-table-column label="开始时间" width="140">
          <template #default="{ row }">{{ formatDate(row.started_at || row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作人" width="100" show-overflow-tooltip>
          <template #default="{ row }">{{ row.operator || '-' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="76" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.status === 'failed'" link type="primary" size="small" @click="retryOne(row as SyncTask)">重试</el-button>
            <span v-else class="op-none">-</span>
          </template>
        </el-table-column>
        <template #empty>
          <div class="queue-empty">
            <p>暂无任务记录</p>
            <span>队列自下一次钉钉同步运行起逐文档记录任务；失败任务可在本页重试。</span>
          </div>
        </template>
      </el-table>

      <!-- 分页 -->
      <div class="pager">
        <el-pagination background layout="total, sizes, prev, pager, next" :total="total"
                       v-model:current-page="page" v-model:page-size="size" :page-sizes="[20, 50, 100]"
                       @current-change="load" @size-change="onSizeChange" />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.queue-page {
  overflow: auto;
}

.queue-card {
  border-radius: 12px;
}

.queue-tabs :deep(.el-tabs__header) {
  margin-bottom: 14px;
}

.tab-count {
  color: var(--el-color-primary);
  font-size: 12px;
}

.filter-bar {
  margin-top: 4px;
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

.expand-toggle {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 2px 0 10px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  cursor: pointer;
  user-select: none;
}

.expand-toggle:hover {
  color: var(--el-color-primary);
}

.batch-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.batch-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.refresh-btn {
  margin-left: auto;
}

.action-tag {
  margin-right: 6px;
}

.task-name {
  font-weight: 600;
}

.op-none {
  color: var(--el-text-color-placeholder);
}

.queue-empty {
  padding: 28px 16px;
  color: var(--el-text-color-secondary);
}

.queue-empty p {
  margin: 0 0 6px;
  font-size: 14px;
  color: var(--el-text-color-regular);
}

.queue-empty span {
  font-size: 12px;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}
</style>
