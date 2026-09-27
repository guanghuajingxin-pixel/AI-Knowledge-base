<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Back } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import { useTabsStore } from '@/stores/tabs'
import ParsedContentView from '@/components/common/ParsedContentView.vue'

// 解析原文独立页面：从分段页「新页面查看」进入，门户页签打开
const route = useRoute()
const router = useRouter()
const tabsStore = useTabsStore()
const libId = computed(() => Number(route.params.libId))
const docId = computed(() => route.params.docId as string)
const docName = ref('')

onMounted(() => {
  const cached = sessionStorage.getItem(`doc_name:${docId.value}`)
  if (cached) docName.value = cached
  tabsStore.updateTabTitle(route.path, `解析原文${docName.value ? ` · ${docName.value}` : ''}`)
})
</script>
<template>
  <div class="parsed-page">
    <div class="page-header">
      <el-button link :icon="Back" @click="router.push({ path: `/apply/knowledge-libraries/${libId}/documents/${docId}` })">返回分段</el-button>
      <el-tag size="small">解析原文</el-tag>
      <span class="doc-name" :title="docName">{{ docName || docId }}</span>
    </div>
    <div class="page-body">
      <ParsedContentView :lib-id="libId" :doc-id="docId" />
    </div>
  </div>
</template>
<style scoped>
.parsed-page { height: 100%; min-height: 0; padding: 16px; box-sizing: border-box; display: flex; flex-direction: column; gap: 12px; }
.page-header { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
.doc-name { font-size: 15px; font-weight: 600; color: #303133; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.page-body { flex: 1; min-height: 0; background: white; border-radius: 8px; overflow: hidden; }
</style>
