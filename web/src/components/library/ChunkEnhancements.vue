<script setup lang="ts">
import { computed } from 'vue'
import { ElAlert } from 'element-plus'
import type { ChunkRetrievalEnhancements } from '@/api/document-library'

const props = defineProps<{ data?: ChunkRetrievalEnhancements }>()
const errors = computed(() => Object.entries(props.data?.errors || {}))
const hasContent = computed(() => Boolean(props.data?.filename || props.data?.summary
  || props.data?.questions?.length || props.data?.image_captions?.length))
const visible = computed(() => hasContent.value || errors.value.length > 0)
const errorLabel: Record<string, string> = {
  summary: '摘要', questions: '候选问题', image_caption: '图片描述',
}
</script>

<template>
  <details v-if="visible" class="chunk-enhancements">
    <summary>
      <span>查看检索增强</span>
      <span class="enhancement-state">{{ hasContent ? '已参与检索' : '生成未完成' }}</span>
    </summary>
    <div class="enhancement-body">
      <div v-if="data?.filename" class="enhancement-row">
        <span class="enhancement-label">文件名</span><span>{{ data.filename }}</span>
      </div>
      <div v-if="data?.summary" class="enhancement-row">
        <span class="enhancement-label">内容摘要</span><span>{{ data.summary }}</span>
      </div>
      <div v-if="data?.questions?.length" class="enhancement-row">
        <span class="enhancement-label">候选问题</span>
        <ul><li v-for="question in data.questions" :key="question">{{ question }}</li></ul>
      </div>
      <div v-if="data?.image_captions?.length" class="enhancement-row">
        <span class="enhancement-label">图片描述</span>
        <ul><li v-for="caption in data.image_captions" :key="`${caption.image}:${caption.caption}`">
          <strong>{{ caption.image }}</strong>：{{ caption.caption }}
        </li></ul>
      </div>
      <el-alert v-if="errors.length" type="warning" :closable="false" show-icon>
        <template #title>部分增强未生成</template>
        <div v-for="[key, message] in errors" :key="key">{{ errorLabel[key] || key }}：{{ message }}</div>
      </el-alert>
    </div>
  </details>
</template>

<style scoped>
.chunk-enhancements { margin-top: 10px; border-top: 1px solid var(--el-border-color-lighter); padding-top: 8px; }
.chunk-enhancements summary { display: flex; align-items: center; gap: 10px; color: var(--el-color-primary); cursor: pointer; font-size: 12px; user-select: none; }
.enhancement-state { color: var(--el-text-color-secondary); }
.enhancement-body { display: grid; gap: 8px; margin-top: 10px; padding: 10px 12px; border-radius: 6px; background: var(--el-fill-color-lighter); color: var(--el-text-color-regular); font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.enhancement-row { display: grid; grid-template-columns: 88px minmax(0, 1fr); gap: 8px; }
.enhancement-label { color: var(--el-text-color-secondary); }
.enhancement-row ul { margin: 0; padding-left: 18px; }
.enhancement-body :deep(.el-alert) { align-items: flex-start; }
</style>
