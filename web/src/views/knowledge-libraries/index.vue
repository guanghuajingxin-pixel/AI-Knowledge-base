<script setup lang="ts">
import { ref, watch, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ExternalLibraries from './ExternalLibraries.vue'
import DocumentLibraries from './DocumentLibraries.vue'
const FaqLibrary = defineAsyncComponent(() => import('@/views/faq/index.vue'))
const MaterialLibraries = defineAsyncComponent(() => import('./MaterialLibraries.vue'))
const route = useRoute()
const router = useRouter()
const activeTab = ref(['document', 'material', 'faq', 'dify', 'ragflow'].includes(String(route.query.type)) ? String(route.query.type) : 'document')
watch(activeTab, type => { void router.replace({ query: { ...route.query, type } }) })
</script>
<template>
  <el-tabs v-model="activeTab" class="library-tabs">
    <el-tab-pane label="文档库" name="document" lazy><DocumentLibraries /></el-tab-pane>
    <el-tab-pane label="物料库" name="material" lazy><MaterialLibraries /></el-tab-pane>
    <el-tab-pane label="问答库" name="faq" lazy><FaqLibrary /></el-tab-pane>
    <el-tab-pane label="DIFY库" name="dify" lazy><ExternalLibraries platform="dify" /></el-tab-pane>
    <el-tab-pane label="RAGFLOW库" name="ragflow" lazy><ExternalLibraries platform="ragflow" /></el-tab-pane>
  </el-tabs>
</template>
<style scoped>
.library-tabs { height: 100%; display: flex; flex-direction: column; }
.library-tabs > :deep(.el-tabs__header) { padding: 8px 20px 0; margin: 0; background: white; }
.library-tabs > :deep(.el-tabs__content) { flex: 1; min-height: 0; overflow: auto; }
.library-tabs > :deep(.el-tabs__content) > .el-tab-pane { height: 100%; }
</style>
