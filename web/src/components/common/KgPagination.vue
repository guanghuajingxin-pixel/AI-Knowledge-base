<script setup lang="ts">
// 统一分页器：全站唯一的分页实现，新页面一律复用本组件，禁止直接维护独立的 el-pagination
// 用法：<KgPagination :page="page" :size="size" :total="total"
//          :sizes="[20, 50]" layout="total, sizes, prev, pager, next"
//          @update:page="..." @update:size="..." />
withDefaults(defineProps<{
  total: number
  page: number
  size: number
  sizes?: number[]   // 每页条数选项；需在 layout 中包含 sizes 段才会显示
  layout?: string
}>(), { layout: 'total, prev, pager, next' })
const emit = defineEmits<{
  (e: 'update:page', v: number): void
  (e: 'update:size', v: number): void
}>()
</script>

<template>
  <el-pagination
    class="kg-pagination"
    background
    :layout="layout"
    :total="total"
    :current-page="page"
    :page-size="size"
    :page-sizes="sizes ?? [20, 50, 100]"
    @update:current-page="(p: number) => emit('update:page', p)"
    @update:page-size="(s: number) => emit('update:size', s)"
  />
</template>

<style scoped>
.kg-pagination {
  margin-top: 14px;
  justify-content: flex-end;
  flex-shrink: 0;
}
</style>
