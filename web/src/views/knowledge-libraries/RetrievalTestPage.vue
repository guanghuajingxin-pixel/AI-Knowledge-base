<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useTabsStore } from '@/stores/tabs'
import RetrievalTestPanel from '@/components/common/RetrievalTestPanel.vue'

const route = useRoute()
const tabsStore = useTabsStore()

const libId = computed(() => Number(route.params.libId))
const docId = computed(() => route.params.docId as string | undefined)
const scope = computed<'document' | 'kb'>(() => docId.value ? 'document' : 'kb')

const docName = ref('')

// 文档级：尝试从 sessionStorage 取文档名用于标题
onMounted(() => {
  if (scope.value === 'document' && docId.value) {
    const cached = sessionStorage.getItem(`doc_name:${docId.value}`)
    if (cached) docName.value = cached
  }
  // 页签标题固定「检索测试」，多开时自动编号 -1/-2…，不带文件名
  tabsStore.assignSequentialTitle(route.path, '检索测试')
})
</script>

<template>
  <div class="retrieval-test-page">
    <RetrievalTestPanel
      backend="library"
      :scope="scope"
      :library-id="libId"
      :document-id="docId"
      :document-title="docName"
      :title="docName"
    />
  </div>
</template>

<style scoped>
.retrieval-test-page {
  height: 100%;
  min-height: 0;
  padding: 16px;
  box-sizing: border-box;
}
</style>
