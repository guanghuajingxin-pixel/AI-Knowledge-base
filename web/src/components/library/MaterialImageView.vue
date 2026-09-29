<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { materialImageBlob } from '@/api/material-library'
const props = defineProps<{ libraryId: number; imageId: string; variant?: 'original' | 'standard' | 'foreground'; version?: string }>()
const url = ref(''), loading = ref(false), failed = ref(false)
let generation = 0
watch(() => [props.libraryId, props.imageId, props.variant, props.version], async () => {
  const ticket = ++generation
  if (url.value) URL.revokeObjectURL(url.value)
  url.value = ''; failed.value = false; loading.value = true
  try {
    const blob = await materialImageBlob(props.libraryId, props.imageId, props.variant || 'standard')
    if (ticket === generation) url.value = URL.createObjectURL(blob)
  } catch { if (ticket === generation) failed.value = true }
  finally { if (ticket === generation) loading.value = false }
}, { immediate: true })
onBeforeUnmount(() => { generation++; if (url.value) URL.revokeObjectURL(url.value) })
</script>
<template>
  <div v-loading="loading" class="material-image-view">
    <el-image v-if="url" :src="url" fit="contain" :preview-src-list="[url]" preview-teleported />
    <span v-else>{{ failed ? '图片暂不可用' : '加载图片' }}</span>
  </div>
</template>
<style scoped>
.material-image-view { height: 220px; min-width: 0; display: flex; align-items: center; justify-content: center; background: #f5f7fa; border-radius: 6px; color: var(--el-color-info); }
.el-image { width: 100%; height: 100%; }
</style>
