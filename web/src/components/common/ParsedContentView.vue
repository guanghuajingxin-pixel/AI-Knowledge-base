<script setup lang="ts">
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { computed, onMounted, ref } from 'vue'
import { getLibraryDocumentParsedContent } from '@/api/document-library'
import { rewriteChunkImages } from '@/utils/chunk-images'

/** 解析原文渲染：JSON 内容格式化展示，markdown 按内容渲染（图片走带鉴权代理）。
 * 分段页右侧面板与新页签页面共用。 */
const props = defineProps<{ libId: number; docId: string }>()

const content = ref('')
const format = ref<'json' | 'markdown'>('markdown')
const loading = ref(false)
const error = ref('')

const prettyJson = computed(() => {
  try { return JSON.stringify(JSON.parse(content.value), null, 2) } catch { return content.value }
})
// 与分段渲染同链路：sanitize 后改写 images/ 相对引用为代理 URL
const renderedMd = computed(() => format.value === 'markdown'
  ? rewriteChunkImages(
      DOMPurify.sanitize(marked.parse(content.value, {async: false}) as string, {FORBID_TAGS: ['iframe', 'video', 'audio']}),
      props.libId, props.docId)
  : '')

onMounted(async () => {
  loading.value = true
  try {
    const res = await getLibraryDocumentParsedContent(props.libId, props.docId)
    content.value = res.content
    format.value = res.format
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e.message || '加载解析原文失败'
  } finally { loading.value = false }
})
</script>
<template>
  <div class="parsed-content-view" v-loading="loading">
    <el-empty v-if="error" :description="error" />
    <pre v-else-if="format === 'json'" class="json-body">{{ prettyJson }}</pre>
    <!-- eslint-disable-next-line vue/no-v-html -->
    <div v-else class="md-body" v-html="renderedMd" />
  </div>
</template>
<style scoped>
.parsed-content-view { height: 100%; overflow: auto; padding: 16px; box-sizing: border-box; }
.json-body {
  margin: 0; padding: 0; font-family: var(--el-font-family-mono, 'SFMono-Regular', Consolas, monospace);
  font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-all; color: #303133;
}
.md-body { line-height: 1.7; color: #303133; overflow-wrap: anywhere; }
.md-body :deep(table) { border-collapse: collapse; width: 100%; }
.md-body :deep(td), .md-body :deep(th) { border: 1px solid var(--el-border-color-lighter); padding: 6px; }
.md-body :deep(img) { max-width: 100%; border-radius: 4px; margin: 6px 0; display: block; }
.md-body :deep(code) { background: #f5f7fa; border-radius: 4px; padding: 2px 6px; font-size: 13px; }
.md-body :deep(pre) { background: #f5f7fa; border-radius: 6px; padding: 12px; overflow: auto; }
</style>
