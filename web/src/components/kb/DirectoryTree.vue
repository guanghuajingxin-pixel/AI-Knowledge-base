<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { getDirectoryTree, createDirectory } from '@/api/knowledge-base'
import type { Directory } from '@/types/knowledge-base'

const props = defineProps<{ kbId: string }>()
const emit = defineEmits<{ (e: 'select', directoryId: string | null): void }>()

const treeData = ref<Directory[]>([])
const loading = ref(false)
const defaultProps = { label: 'name', children: 'children' }
const selectedId = ref<string | null>(null)
const showAddInput = ref(false)
const newDirName = ref('')
const addingToParent = ref<string | null>(null)

async function fetchTree() {
  loading.value = true
  try {
    treeData.value = await getDirectoryTree(props.kbId)
  } finally {
    loading.value = false
  }
}

function handleNodeClick(data: Directory) {
  selectedId.value = data.id
  emit('select', data.id)
}

function selectAll() {
  selectedId.value = null
  emit('select', null)
}

async function addDirectory(parentId: string | null) {
  addingToParent.value = parentId
  showAddInput.value = true
  newDirName.value = ''
}

async function confirmAdd() {
  if (!newDirName.value.trim()) return
  await createDirectory(props.kbId, { name: newDirName.value, parent_id: addingToParent.value })
  ElMessage.success('目录已创建')
  showAddInput.value = false
  fetchTree()
}

onMounted(fetchTree)
watch(() => props.kbId, fetchTree)
</script>

<template>
  <div class="directory-tree" v-loading="loading">
    <div class="tree-header">
      <span>目录</span>
      <el-icon class="add-icon" @click="addDirectory(null)"><Plus /></el-icon>
    </div>
    <el-input
      v-if="showAddInput"
      v-model="newDirName"
      size="small"
      placeholder="目录名称"
      @keyup.enter="confirmAdd"
      @blur="showAddInput = false"
    />
    <el-tree
      :data="treeData"
      :props="defaultProps"
      node-key="id"
      highlight-current
      @node-click="handleNodeClick"
    >
      <template #default="{ data }">
        <span class="tree-node">
          <el-icon><Folder /></el-icon>
          <span class="node-label">{{ data.name }}</span>
          <el-icon class="node-add" @click.stop="addDirectory(data.id)"><Plus /></el-icon>
        </span>
      </template>
    </el-tree>
    <div class="all-docs" :class="{ active: !selectedId }" @click="selectAll">
      <el-icon><FolderOpened /></el-icon>
      <span>全部文档</span>
    </div>
  </div>
</template>

<style scoped>
.directory-tree { width: 240px; border-right: 1px solid #e6e6e6; padding: 12px; }
.tree-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; font-weight: 600; }
.add-icon { cursor: pointer; color: var(--el-color-primary); }
.tree-node { display: flex; align-items: center; gap: 4px; flex: 1; }
.node-label { flex: 1; }
.node-add { cursor: pointer; color: var(--el-color-primary); opacity: 0; }
.tree-node:hover .node-add { opacity: 1; }
.all-docs { display: flex; align-items: center; gap: 6px; padding: 6px 8px; cursor: pointer; border-radius: 4px; margin-top: 8px; }
.all-docs.active { background: var(--el-color-primary-light-9); color: var(--el-color-primary); }
</style>
