<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import PageContainer from '@/components/common/PageContainer.vue'
import DirectoryTree from '@/components/kb/DirectoryTree.vue'
import AddKnowledgeDialog from '@/components/kb/AddKnowledgeDialog.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { getKnowledgeBase } from '@/api/knowledge-base'
import { listDocuments, deleteDocument, getDocumentPreviewUrl, reprocessDocument } from '@/api/document'
import type { KnowledgeBase } from '@/types/knowledge-base'
import type { Document } from '@/types/document'
import { formatFileSize, formatDate } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const kbId = route.params.id as string

const kb = ref<KnowledgeBase | null>(null)
const documents = ref<Document[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(10)
const loading = ref(false)
const selectedDirectory = ref<string | null>(null)
const addKnowledgeVisible = ref(false)

async function fetchKb() {
  kb.value = await getKnowledgeBase(kbId)
}

async function fetchDocuments() {
  loading.value = true
  try {
    const res = await listDocuments({
      kb_id: kbId,
      directory_id: selectedDirectory.value || undefined,
      page: page.value,
      size: size.value,
    })
    documents.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

function handleDirectorySelect(dirId: string | null) {
  selectedDirectory.value = dirId
  page.value = 1
  fetchDocuments()
}

function handlePageChange(p: number) {
  page.value = p
  fetchDocuments()
}

async function handleDelete(id: string) {
  await ElMessageBox.confirm('确认删除该文档及其所有切片和索引？', '警告', { type: 'warning' })
  await deleteDocument(id)
  ElMessage.success('删除成功')
  fetchDocuments()
}

async function handlePreview(id: string) {
  const res = await getDocumentPreviewUrl(id)
  window.open(res.preview_url, '_blank')
}

async function handleReprocess(id: string) {
  await reprocessDocument(id)
  ElMessage.success('已提交重新处理')
  fetchDocuments()
}

function goSegments(docId: string) {
  router.push(`/knowledge-bases/${kbId}/documents/${docId}`)
}

onMounted(() => {
  fetchKb()
  fetchDocuments()
})
</script>

<template>
  <PageContainer :title="kb?.name || '知识库详情'">
    <template #actions>
      <el-button :icon="Refresh" @click="fetchDocuments">刷新</el-button>
      <el-button type="primary" :icon="Plus" @click="addKnowledgeVisible = true">添加知识</el-button>
    </template>
    <div class="detail-layout">
      <DirectoryTree :kb-id="kbId" @select="handleDirectorySelect" />
      <div class="doc-section">
        <el-table :data="documents" v-loading="loading" style="width: 100%">
          <el-table-column prop="original_filename" label="文件名" min-width="200" show-overflow-tooltip />
          <el-table-column prop="file_type" label="类型" width="80" />
          <el-table-column label="大小" width="100">
            <template #default="{ row }">{{ formatFileSize(row.file_size) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }"><StatusTag :status="row.status" /></template>
          </el-table-column>
          <el-table-column prop="chunk_count" label="切片数" width="80" />
          <el-table-column label="创建时间" width="160">
            <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="220" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="goSegments(row.id)">切片</el-button>
              <el-button link type="primary" size="small" @click="handlePreview(row.id)">预览</el-button>
              <el-button link type="warning" size="small" @click="handleReprocess(row.id)">重处理</el-button>
              <el-button link type="danger" size="small" @click="handleDelete(row.id)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          v-if="total > size"
          class="pagination"
          background
          layout="total, prev, pager, next"
          :total="total"
          :page-size="size"
          :current-page="page"
          @current-change="handlePageChange"
        />
      </div>
    </div>
  </PageContainer>
  <AddKnowledgeDialog v-model="addKnowledgeVisible" :kb-id="kbId" :directory-id="selectedDirectory" @success="fetchDocuments" />
</template>

<style scoped>
.detail-layout { display: flex; gap: 16px; }
.doc-section { flex: 1; }
.pagination { margin-top: 16px; justify-content: flex-end; }
</style>
