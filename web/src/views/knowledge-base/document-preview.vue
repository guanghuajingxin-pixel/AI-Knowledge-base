<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft } from '@element-plus/icons-vue'
import PageContainer from '@/components/common/PageContainer.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { getDocument, getDocumentSegments } from '@/api/document'
import type { Document, Segment } from '@/types/document'
import { formatFileSize, formatDate } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const docId = route.params.docId as string
const kbId = route.params.id as string

const doc = ref<Document | null>(null)
const segments = ref<Segment[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(10)
const loading = ref(false)

async function fetchDoc() {
  doc.value = await getDocument(docId)
}

async function fetchSegments() {
  loading.value = true
  try {
    const res = await getDocumentSegments(docId, { page: page.value, size: size.value })
    segments.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

function handlePageChange(p: number) {
  page.value = p
  fetchSegments()
}

onMounted(() => {
  fetchDoc()
  fetchSegments()
})
</script>

<template>
  <PageContainer :title="doc?.original_filename || '切片预览'">
    <template #actions>
      <el-button :icon="ArrowLeft" @click="router.push(`/knowledge-bases/${kbId}`)">返回</el-button>
    </template>
    <div v-if="doc" class="doc-info">
      <el-descriptions :column="4" border size="small">
        <el-descriptions-item label="文件名">{{ doc.original_filename }}</el-descriptions-item>
        <el-descriptions-item label="类型">{{ doc.file_type }}</el-descriptions-item>
        <el-descriptions-item label="大小">{{ formatFileSize(doc.file_size) }}</el-descriptions-item>
        <el-descriptions-item label="状态"><StatusTag :status="doc.status" /></el-descriptions-item>
        <el-descriptions-item label="切片数">{{ doc.chunk_count }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ formatDate(doc.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="错误信息" :span="2">{{ doc.error_message || '无' }}</el-descriptions-item>
      </el-descriptions>
    </div>
    <div class="segment-list" v-loading="loading" style="margin-top: 16px;">
      <div v-for="seg in segments" :key="seg.id" class="segment-item">
        <div class="seg-header">
          <span class="seg-index">#{{ seg.chunk_index + 1 }}</span>
          <span class="seg-meta">字符数: {{ seg.content.length }}</span>
          <span class="seg-meta">token: {{ seg.token_count }}</span>
          <span class="seg-meta">hash: {{ seg.content_hash.slice(0, 16) }}...</span>
        </div>
        <div class="seg-content">{{ seg.content }}</div>
      </div>
    </div>
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
  </PageContainer>
</template>

<style scoped>
.doc-info { background: #fafafa; padding: 16px; border-radius: 4px; }
.segment-item { border: 1px solid #ebeef5; border-radius: 4px; margin-bottom: 12px; overflow: hidden; }
.seg-header { display: flex; align-items: center; gap: 12px; padding: 8px 16px; background: #f5f7fa; border-bottom: 1px solid #ebeef5; }
.seg-index { font-weight: 600; color: var(--el-color-primary); }
.seg-meta { font-size: 12px; color: #909399; }
.seg-content { padding: 16px; font-size: 14px; line-height: 1.8; color: #303133; white-space: pre-wrap; }
.pagination { margin-top: 16px; justify-content: flex-end; }
</style>
