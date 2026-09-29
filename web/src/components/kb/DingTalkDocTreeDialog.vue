<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { fetchDingTalkWorkspaces, fetchDingTalkNodes } from '@/api/knowledge-center'
import type { DingTalkWorkspace, DingTalkNode } from '@/types/knowledge-center'

interface TreeNode {
  node_id: string
  name: string
  is_folder: boolean
  /** el-tree isLeaf：true=叶子节点（无展开箭头） */
  leaf: boolean
  extension: string | null
  size: number
  url: string | null
  children?: TreeNode[]
  loading?: boolean
}

interface TreeNodeProps {
  label: string
  children: string
  isLeaf: string
}

defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', val: boolean): void
  (e: 'selected', sel: {
    workspaceId: string
    workspaceName: string
    rootNodeId: string
    nodes: Array<{ node_id: string; name: string; workspace_id: string }>
  }): void
}>()

// 知识库列表
const workspaces = ref<DingTalkWorkspace[]>([])
const wsLoading = ref(false)
const wsError = ref('')
const workspaceId = ref('')
const workspaceName = computed(() =>
  workspaces.value.find((w) => w.id === workspaceId.value)?.name || '',
)
const workspaceRootId = computed(() =>
  workspaces.value.find((w) => w.id === workspaceId.value)?.root_node_id || '',
)

// 搜索
const searchKeyword = ref('')
// 选中结果
const selectedNodes = ref<TreeNode[]>([])

// 浏览器内存缓存：5 分钟，避免重复请求钉钉
const CACHE_TTL = 5 * 60 * 1000
type NodeCache = { ts: number; items: DingTalkNode[] }
const nodesCache = new Map<string, NodeCache>()

// localStorage 持久缓存（1 小时）：钉钉知识库列表极少变化
const WS_STORE_KEY = 'kge:dingtalk_workspaces_v1'
const WS_STORE_TTL = 3_600_000

const treeProps: TreeNodeProps = {
  label: 'name',
  children: 'children',
  isLeaf: 'leaf',
}

const treeRef = ref<any>()

async function loadWorkspaces(force = false) {
  // 持久缓存命中：秒开，后台校准
  if (!force) {
    try {
      const raw = localStorage.getItem(WS_STORE_KEY)
      if (raw) {
        const c = JSON.parse(raw) as { ts: number; items: DingTalkWorkspace[] }
        if (Date.now() - c.ts <= WS_STORE_TTL && c.items?.length) {
          workspaces.value = c.items
          workspaceId.value = c.items[0]?.id || ''
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
  wsLoading.value = true
  wsError.value = ''
  try {
    const res = await fetchDingTalkWorkspaces()
    workspaces.value = res.items || []
    wsError.value = res.error || ''
    if (workspaces.value.length) {
      workspaceId.value = workspaces.value[0].id
      try { localStorage.setItem(WS_STORE_KEY, JSON.stringify({ ts: Date.now(), items: workspaces.value })) } catch { /* 忽略 */ }
    }
  } catch (e: any) {
    wsError.value = e?.response?.data?.detail || e?.message || '获取钉钉知识库失败'
  } finally {
    wsLoading.value = false
  }
}

function changeWorkspace() {
  selectedNodes.value = []
}

// 懒加载树节点
async function loadNode(node: any, resolve: (data: TreeNode[]) => void) {
  // 根级：从知识库根节点开始
  const parentId: string = node.level === 0 ? workspaceRootId.value : node.data.node_id
  if (!parentId) { resolve([]); return }

  const cached = nodesCache.get(parentId)
  let items: DingTalkNode[]
  if (cached && Date.now() - cached.ts < CACHE_TTL) {
    items = cached.items
  } else {
    try {
      const res = await fetchDingTalkNodes(parentId)
      items = res.items || []
      nodesCache.set(parentId, { ts: Date.now(), items })
    } catch (e: any) {
      ElMessage.error(e?.response?.data?.detail || e?.message || '获取子节点失败')
      items = []
    }
  }
  resolve(items.map((n) => ({
    node_id: n.node_id,
    name: n.name,
    is_folder: n.is_folder,
    leaf: !n.has_children,
    extension: n.extension,
    size: n.size,
    url: n.url,
  })))
}

// el-tree 的 show-checkbox + check 事件
function handleCheck() {
  // 只保留文件类型（非文件夹）的选中
  const checked = treeRef.value?.getCheckedNodes(false, false) as TreeNode[] || []
  selectedNodes.value = checked.filter((n) => !n.is_folder)
}

// 过滤搜索（高亮 + 展开匹配节点）
watch(searchKeyword, (kw) => {
  treeRef.value?.filter(kw)
})

function filterNode(value: string, data: any) {
  if (!value) return true
  return data.name.toLowerCase().includes(value.toLowerCase())
}

function handleConfirm() {
  if (!selectedNodes.value.length) {
    ElMessage.warning('请选择至少一个文档')
    return
  }
  emit('selected', {
    workspaceId: workspaceId.value,
    workspaceName: workspaceName.value,
    rootNodeId: workspaceRootId.value,
    nodes: selectedNodes.value.map((n) => ({
      node_id: n.node_id,
      name: n.name,
      workspace_id: workspaceId.value,
    })),
  })
  emit('update:modelValue', false)
}

function handleClosed() {
  selectedNodes.value = []
  searchKeyword.value = ''
}

onMounted(loadWorkspaces)
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="选择文档"
    width="680px"
    class="dt-doc-dialog"
    @update:model-value="emit('update:modelValue', $event)"
    @closed="handleClosed"
  >
    <div class="dialog-content">
      <!-- 知识库选择 + 搜索 -->
      <div class="toolbar">
        <el-select
          v-model="workspaceId"
          :loading="wsLoading"
          placeholder="选择钉钉知识库"
          style="width: 240px"
          @change="changeWorkspace"
        >
          <el-option
            v-for="ws in workspaces"
            :key="ws.id"
            :label="ws.name"
            :value="ws.id"
          />
        </el-select>
        <el-input
          v-model="searchKeyword"
          :prefix-icon="Search"
          placeholder="搜索文件名"
          clearable
          style="width: 200px"
        />
      </div>

      <el-alert
        v-if="wsError"
        :title="wsError"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
      />

      <!-- 树 -->
      <div class="tree-container" v-loading="wsLoading && !workspaces.length">
        <el-tree
          v-if="workspaceId"
          ref="treeRef"
          :key="workspaceId"
          :props="treeProps"
          :load="loadNode"
          :filter-node-method="filterNode"
          lazy
          show-checkbox
          node-key="node_id"
          :default-expanded-keys="[]"
          @check="handleCheck"
        >
          <template #default="{ data }">
            <span class="tree-node">
              <span class="node-name">{{ data.name }}</span>
              <el-tag
                v-if="!data.is_folder && data.extension"
                size="small"
                effect="plain"
                class="node-ext"
              >
                {{ data.extension.toUpperCase() }}
              </el-tag>
            </span>
          </template>
        </el-tree>
        <el-empty
          v-else
          description="请先选择一个钉钉知识库"
          :image-size="80"
        />
      </div>

      <!-- 已选统计 -->
      <div class="selected-bar">
        <span>已选 {{ selectedNodes.length }} 个文档</span>
      </div>
    </div>

    <template #footer>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button
        type="primary"
        :disabled="!selectedNodes.length"
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

.toolbar {
  display: flex;
  gap: 12px;
  align-items: center;
}

.tree-container {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 8px;
  max-height: 400px;
  overflow-y: auto;
  min-height: 280px;
}

.tree-node {
  display: flex;
  align-items: center;
  gap: 8px;
}

.node-name {
  font-size: 14px;
}

.node-ext {
  transform: scale(0.85);
}

.selected-bar {
  font-size: 13px;
  color: #606266;
  padding: 4px 0;
}
</style>
