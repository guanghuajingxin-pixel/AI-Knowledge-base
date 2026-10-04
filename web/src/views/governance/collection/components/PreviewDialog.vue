<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { syncSource, updatePreviewSettings } from '@/api/sync'
import type { PreviewItem } from '@/types/sync'

const props = defineProps<{ visible: boolean; name: string; sourceId: number; items: PreviewItem[]; loading?: boolean }>()
const emit = defineEmits<{ (e: 'update:visible', v: boolean): void; (e: 'done'): void; (e: 'refresh'): void }>()
const running = ref(false)

// 前端分页：预演结果可能上千行，表格只渲染当前页
const page = ref(1)
const pageSize = 20
const pagedItems = computed(() =>
  props.items.slice((page.value - 1) * pageSize, page.value * pageSize))
watch(() => props.visible, (v) => { if (v) page.value = 1 })
// 刷新后数据整体替换，回到第一页
watch(() => props.items, () => { page.value = 1 })

function badge(action: string) {
  if (action === '新增') return 'ok'
  if (action === '更新') return 'run'
  if (action === '删除') return 'warn'
  return 'off'
}
async function confirm() {
  running.value = true
  try {
    await updatePreviewSettings(props.sourceId, props.items.filter((item) => item.node_id).map((item) => ({
      node_id: item.node_id!, name: item.name || item.doc, category: item.category || 'DOCUMENT', enabled: item.enabled !== false,
    })))
    const res = await syncSource(props.sourceId) as { message?: string } | undefined
    ElMessage.success(res?.message || '同步任务已启动，可在同步队列查看结果')
    emit('done')
    emit('update:visible', false)
  } finally { running.value = false }
}
</script>

<template>
  <el-dialog :model-value="visible" width="680px" :title="`同步列表 · ${name}`" @close="emit('update:visible', false)">
    <div style="font-size:13px;color:var(--el-text-color-secondary);margin-bottom:10px">
      『新增』＝未同步过；『更新』＝上一轮已同步；『删除』＝钉钉侧已移除。开关打开的文档本次都会重新同步。目录快照缓存 10 分钟，「刷新列表」强制重新拉取。
    </div>
    <el-table :data="pagedItems" size="small" border>
      <el-table-column label="动作" width="80">
        <template #default="{ row }">
          <span class="preview-pill" :class="badge(row.action)">{{ row.action }}</span>
        </template>
      </el-table-column>
      <el-table-column label="文档" prop="doc" min-width="320" show-overflow-tooltip />
      <el-table-column label="操作" width="140">
        <template #default="{ row }">
          <div v-if="row.node_id" class="preview-toggle">
            <el-switch v-model="row.enabled" :aria-label="`${row.doc}参与同步`" />
            <span>{{ row.enabled ? '参与同步' : '不参与' }}</span>
          </div>
          <span v-else style="font-size:12px;color:var(--el-text-color-secondary)">—</span>
        </template>
      </el-table-column>
      <template #empty><div style="padding:20px;color:var(--el-text-color-secondary)">该目录下没有可同步的文档</div></template>
    </el-table>
    <div style="display:flex;justify-content:flex-end;margin-top:10px">
      <el-pagination
        v-model:current-page="page"
        :total="items.length"
        :page-size="pageSize"
        layout="total, prev, pager, next, jumper"
        background
        size="small"
      />
    </div>
    <template #footer>
      <div style="display:flex;justify-content:space-between;align-items:center;width:100%">
        <el-tooltip content="绕过缓存，重新遍历钉钉目录树（会保留已调整的开关状态）" placement="top">
          <el-button :loading="props.loading" @click="emit('refresh')">刷新列表</el-button>
        </el-tooltip>
        <div>
          <el-button @click="emit('update:visible', false)">关闭</el-button>
          <el-button type="primary" :loading="running" :disabled="props.loading" @click="confirm">确认并开始同步</el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.preview-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}
.preview-pill {
  display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; line-height: 18px;
  border: 1px solid var(--el-border-color); color: var(--el-text-color-secondary); background: var(--el-fill-color-light);
}
.preview-pill.ok { color: #67c23a; border-color: #b3e19d; background: #f0f9eb; }
.preview-pill.run { color: var(--el-color-primary); border-color: var(--el-color-primary-light-5); background: var(--el-color-primary-light-9); }
.preview-pill.warn { color: #e6a23c; border-color: #f5dab1; background: #fdf6ec; }
.preview-pill.off { color: #909399; border-color: #d3d4d6; background: #f4f4f5; }
</style>
