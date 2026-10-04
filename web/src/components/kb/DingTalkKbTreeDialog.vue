<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Search, FolderOpened } from '@element-plus/icons-vue'
import { fetchDingTalkWorkspaces } from '@/api/knowledge-center'
import type { DingTalkWorkspace } from '@/types/knowledge-center'

defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', val: boolean): void
  (e: 'selected', sel: {
    workspaces: Array<{ id: string; name: string; root_node_id: string }>
  }): void
}>()

const workspaces = ref<DingTalkWorkspace[]>([])
const loading = ref(false)
const error = ref('')
const searchKeyword = ref('')
const selectedIds = ref<string[]>([])

// localStorage 持久缓存
const WS_STORE_KEY = 'kge:dingtalk_workspaces_v1'
const WS_STORE_TTL = 3_600_000

const filteredWorkspaces = computed(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  return kw
    ? workspaces.value.filter((w) => w.name.toLowerCase().includes(kw))
    : workspaces.value
})

function isSelected(id: string) {
  return selectedIds.value.includes(id)
}

function toggleSelect(ws: DingTalkWorkspace) {
  const idx = selectedIds.value.indexOf(ws.id)
  if (idx >= 0) {
    selectedIds.value.splice(idx, 1)
  } else {
    selectedIds.value.push(ws.id)
  }
}

async function loadWorkspaces(force = false) {
  if (!force) {
    try {
      const raw = localStorage.getItem(WS_STORE_KEY)
      if (raw) {
        const c = JSON.parse(raw) as { ts: number; items: DingTalkWorkspace[] }
        if (Date.now() - c.ts <= WS_STORE_TTL && c.items?.length) {
          workspaces.value = c.items
          // 后台静默校准
          fetchDingTalkWorkspaces().then((res) => {
            const items = res.items || []
            if (items.length) {
              workspaces.value = items
              try { localStorage.setItem(WS_STORE_KEY, JSON.stringify({ ts: Date.now(), items })) } catch { /* 忽略 */ }
            }
          }).catch(() => { /* 保留缓存 */ })
          return
        }
      }
    } catch { /* 缓存读取失败 */ }
  }
  loading.value = true
  error.value = ''
  try {
    const res = await fetchDingTalkWorkspaces()
    workspaces.value = res.items || []
    error.value = res.error || ''
    if (workspaces.value.length) {
      try { localStorage.setItem(WS_STORE_KEY, JSON.stringify({ ts: Date.now(), items: workspaces.value })) } catch { /* 忽略 */ }
    }
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '获取钉钉知识库失败'
  } finally {
    loading.value = false
  }
}

function handleConfirm() {
  if (!selectedIds.value.length) return
  const selected = selectedIds.value
    .map((id) => workspaces.value.find((w) => w.id === id))
    .filter(Boolean) as DingTalkWorkspace[]
  emit('selected', {
    workspaces: selected.map((w) => ({
      id: w.id,
      name: w.name,
      root_node_id: w.root_node_id,
    })),
  })
  emit('update:modelValue', false)
}

function handleClosed() {
  selectedIds.value = []
  searchKeyword.value = ''
}

onMounted(loadWorkspaces)
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="选择知识库"
    width="640px"
    class="dt-kb-dialog"
    @update:model-value="emit('update:modelValue', $event)"
    @closed="handleClosed"
  >
    <div class="dialog-content">
      <!-- 搜索栏 -->
      <div class="search-bar">
        <el-input
          v-model="searchKeyword"
          :prefix-icon="Search"
          placeholder="搜索知识库名称"
          clearable
          style="width: 100%"
        />
      </div>

      <el-alert
        v-if="error"
        :title="error"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
      />

      <!-- 知识库列表 -->
      <div class="kb-list" v-loading="loading">
        <template v-if="filteredWorkspaces.length">
          <div
            v-for="ws in filteredWorkspaces"
            :key="ws.id"
            class="kb-item"
            :class="{ selected: isSelected(ws.id) }"
            @click="toggleSelect(ws)"
          >
            <div class="kb-icon">
              <el-icon><FolderOpened /></el-icon>
            </div>
            <div class="kb-body">
              <div class="kb-name">{{ ws.name }}</div>
              <div class="kb-desc">暂无简介</div>
            </div>
            <el-icon v-if="isSelected(ws.id)" class="check-icon"><svg viewBox="0 0 1024 1024" width="16" height="16"><path fill="currentColor" d="M369.8 820.2 109.5 559.9l56.6-56.6 203.7 203.7L857.9 219l56.6 56.6z"/></svg></el-icon>
          </div>
        </template>
        <el-empty
          v-else-if="!loading"
          description="暂无知识库"
          :image-size="80"
        />
      </div>

      <!-- 已选统计 -->
      <div class="selected-bar">
        <span>已选 {{ selectedIds.length }} 个知识库</span>
      </div>
    </div>

    <template #footer>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button
        type="primary"
        :disabled="!selectedIds.length"
        @click="handleConfirm"
      >
        确认
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.dialog-content {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.search-bar {
  flex-shrink: 0;
}

.kb-list {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  max-height: 380px;
  overflow-y: auto;
  min-height: 260px;
  padding: 4px;
}

.kb-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;
}

.kb-item:hover {
  background: #f5f7fa;
}

.kb-item.selected {
  background: var(--el-color-primary-light-9);
}

.kb-icon {
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  flex-shrink: 0;
}

.kb-body {
  flex: 1;
  min-width: 0;
}

.kb-name {
  font-size: 14px;
  color: #303133;
  font-weight: 500;
  margin-bottom: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-desc {
  font-size: 12px;
  color: #c0c4cc;
}

.check-icon {
  color: var(--el-color-primary);
  font-size: 16px;
  flex-shrink: 0;
}

.selected-bar {
  font-size: 13px;
  color: #606266;
  padding: 4px 0;
}
</style>
