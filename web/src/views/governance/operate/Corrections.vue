<template>
  <div class="kge-page corr-page">
    <!-- 过滤栏 -->
    <div class="filter-bar">
      <el-input
        v-model="filters.keyword"
        placeholder="请输入知识标题"
        clearable
        style="width: 280px"
        :prefix-icon="Search"
        @keyup.enter="onSearch"
      />
      <el-select v-model="filters.error_type" placeholder="错误类型" clearable style="width: 160px" @change="onSearch">
        <el-option v-for="t in errorTypes" :key="t" :label="t" :value="t" />
      </el-select>
      <el-select v-model="filters.status" placeholder="状态" clearable style="width: 140px" @change="onSearch">
        <el-option label="待处理" value="pending" />
        <el-option label="处理中" value="processing" />
        <el-option label="已解决" value="resolved" />
        <el-option label="已关闭" value="closed" />
      </el-select>
      <el-button type="primary" @click="onSearch">查询</el-button>
      <el-button @click="onReset">重置</el-button>
    </div>

    <!-- 工单表格：撑满剩余高度，表体内部滚动（kge-fill 约定） -->
    <div class="table-fill">
      <el-table v-loading="loading" :data="items" class="corr-table" height="100%" empty-text="暂无数据">
      <el-table-column label="知识标题" min-width="240" show-overflow-tooltip>
        <template #default="{ row }">
          <a v-if="(row as CorrectionItem).knowledge_url"
             class="kb-title kb-link" :href="(row as CorrectionItem).knowledge_url"
             target="_blank" rel="noopener" :title="'点击打开原文：' + (row as CorrectionItem).knowledge_url">
            {{ (row as CorrectionItem).knowledge_title }}
          </a>
          <div v-else class="kb-title">{{ (row as CorrectionItem).knowledge_title }}</div>
          <div v-if="(row as CorrectionItem).question" class="kb-q">相关问题：{{ (row as CorrectionItem).question }}</div>
        </template>
      </el-table-column>
      <el-table-column prop="error_type" label="错误类型" width="120" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType((row as CorrectionItem).status)">{{ (row as CorrectionItem).status_label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="applicant" label="申请人" width="120" />
      <el-table-column label="处理人" width="120">
        <template #default="{ row }">{{ (row as CorrectionItem).handler || '-' }}</template>
      </el-table-column>
      <el-table-column prop="created_at" label="提交时间" width="150" />
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="viewDetail(row as CorrectionItem)">详情</el-button>
          <el-button
            v-if="(row as CorrectionItem).status === 'pending' || (row as CorrectionItem).status === 'processing'"
            link type="warning" size="small" @click="openHandle(row as CorrectionItem)"
          >处理</el-button>
        </template>
      </el-table-column>
      </el-table>
    </div>

    <!-- 分页 -->
    <KgPagination
      v-if="total > 0"
      :page="page"
      :size="pageSize"
      :total="total"
      @update:page="onPageChange"
      @update:size="onPageSizeChange"
    />

    <!-- 详情弹窗 -->
    <el-dialog v-model="detailVisible" title="纠错详情" width="560px">
      <template v-if="current">
        <el-descriptions :column="1" border>
          <el-descriptions-item label="知识标题">
          <a v-if="current.knowledge_url" :href="current.knowledge_url" target="_blank" rel="noopener" class="kb-link">
            {{ current.knowledge_title }} 🔗
          </a>
          <span v-else>{{ current.knowledge_title }}</span>
        </el-descriptions-item>
          <el-descriptions-item label="错误类型">{{ current.error_type }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ current.status_label }}</el-descriptions-item>
          <el-descriptions-item label="申请人">{{ current.applicant }}</el-descriptions-item>
          <el-descriptions-item label="相关问题">{{ current.question || '-' }}</el-descriptions-item>
          <el-descriptions-item label="纠错说明">
            <div class="detail-content">{{ current.content || '（无）' }}</div>
          </el-descriptions-item>
          <el-descriptions-item v-if="current.handler_note" label="处理备注">{{ current.handler_note }}</el-descriptions-item>
          <el-descriptions-item label="提交时间">{{ current.created_at }}</el-descriptions-item>
        </el-descriptions>
      </template>
    </el-dialog>

    <!-- 处理弹窗 -->
    <el-dialog v-model="handleVisible" title="处理纠错工单" width="480px">
      <el-form label-width="80px">
        <el-form-item label="知识标题">
          <span class="handle-title">{{ current?.knowledge_title }}</span>
        </el-form-item>
        <el-form-item label="处理状态">
          <el-select v-model="handleForm.status" style="width: 100%">
            <el-option label="处理中" value="processing" />
            <el-option label="已解决" value="resolved" />
            <el-option label="已关闭" value="closed" />
          </el-select>
        </el-form-item>
        <el-form-item label="处理备注">
          <el-input v-model="handleForm.handler_note" type="textarea" :rows="4" placeholder="记录处理过程/结果" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="handleVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitHandle">提交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import KgPagination from '@/components/common/KgPagination.vue'
import { getCorrections, updateCorrection, type CorrectionItem } from '@/api/qa'

const loading = ref(false)
const items = ref<CorrectionItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const errorTypes = ref<string[]>([])

const filters = reactive({ keyword: '', error_type: '', status: '' })

const detailVisible = ref(false)
const handleVisible = ref(false)
const submitting = ref(false)
const current = ref<CorrectionItem | null>(null)
const handleForm = reactive({ status: 'resolved', handler_note: '' })

function statusType(s: string): 'danger' | 'warning' | 'success' | 'info' {
  return ({ pending: 'danger', processing: 'warning', resolved: 'success', closed: 'info' } as const)[s] || 'info'
}

async function load() {
  loading.value = true
  try {
    const res = await getCorrections({
      keyword: filters.keyword || undefined,
      error_type: filters.error_type || undefined,
      status: filters.status || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    items.value = res.items
    total.value = res.total
    errorTypes.value = res.error_types || []
  } finally {
    loading.value = false
  }
}

function onSearch() {
  page.value = 1
  load()
}
function onReset() {
  filters.keyword = ''
  filters.error_type = ''
  filters.status = ''
  page.value = 1
  load()
}
function onPageChange(p: number) {
  page.value = p
  load()
}
function onPageSizeChange(s: number) {
  pageSize.value = s
  page.value = 1
  load()
}

function viewDetail(row: CorrectionItem) {
  current.value = row
  detailVisible.value = true
}

function openHandle(row: CorrectionItem) {
  current.value = row
  handleForm.status = row.status === 'pending' ? 'processing' : row.status
  handleForm.handler_note = row.handler_note || ''
  handleVisible.value = true
}

async function submitHandle() {
  if (!current.value) return
  submitting.value = true
  try {
    await updateCorrection(current.value.id, {
      status: handleForm.status,
      handler_note: handleForm.handler_note,
    })
    ElMessage.success('处理结果已提交')
    handleVisible.value = false
    load()
  } finally {
    submitting.value = false
  }
}

onMounted(load)
</script>

<style scoped>
/* kge-page 为 flex 纵向容器：过滤栏/分页固定，表格撑满剩余高度（kge-fill 约定） */
.filter-bar {
  display: flex; align-items: center; gap: 12px;
  background: #fff; border: 1px solid var(--line, #E5E7EB); border-radius: 10px;
  padding: 14px 16px; margin-bottom: 14px;
  flex-shrink: 0;
}
.table-fill { flex: 1; min-height: 0; }
.corr-table {
  background: #fff; border-radius: 10px; border: 1px solid var(--line, #E5E7EB);
}
.kb-title { font-weight: 600; color: #1F2937; font-size: 13.5px; }
a.kb-link { text-decoration: none; }
a.kb-link:hover { color: #2563EB; text-decoration: underline; }
.kb-link { color: #2563EB; }
.kb-q { font-size: 12px; color: #9CA3AF; margin-top: 2px; }
.detail-content { white-space: pre-wrap; line-height: 1.6; }
.handle-title { font-weight: 600; color: #374151; }
</style>
