<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ status: string }>()
const map: Record<string, [string, string]> = {
  ok: ['ok', '正常'],
  err: ['err', '有失败'],
  off: ['off', '停用'],
  run: ['run', '运行中'],
  success: ['ok', '成功'],
  partial: ['warn', '部分成功'],
  failed: ['err', '失败'],
  running: ['run', '运行中'],
  enabled: ['ok', '启用'],
  disabled: ['off', '停用'],
}
const info = computed<[string, string]>(() => map[props.status] || ['off', props.status])
</script>

<template>
  <span class="sync-pill" :class="info[0]">{{ info[1] }}</span>
</template>

<style scoped>
.sync-pill {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 12px;
  line-height: 18px;
  border: 1px solid var(--el-border-color);
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
}
.sync-pill.ok { color: #67c23a; border-color: #b3e19d; background: #f0f9eb; }
.sync-pill.err { color: #f56c6c; border-color: #fab6b6; background: #fef0f0; }
.sync-pill.warn { color: #e6a23c; border-color: #f5dab1; background: #fdf6ec; }
.sync-pill.run { color: var(--el-color-primary); border-color: var(--el-color-primary-light-5); background: var(--el-color-primary-light-9); }
.sync-pill.off { color: #909399; border-color: #d3d4d6; background: #f4f4f5; }
</style>
