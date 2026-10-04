<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { CascaderOption } from 'element-plus'
import { listDifyDatasets, listSupportedExtensions, syncDingTalkFile, type DifyDataset } from '@/api/dify'
import { fileIcon } from '@/utils/file-icon'
import { fetchDingTalkWorkspaces, fetchDingTalkNodes } from '@/api/knowledge-center'
import type { DingTalkWorkspace, DingTalkNode } from '@/types/knowledge-center'
import Sources from './Sources.vue'
import PipelineInputs from '@/components/collection/PipelineInputs.vue'

const activeTab = ref('dirs')
const datasets = ref<DifyDataset[]>([])
const datasetsLoading = ref(false)
const datasetsError = ref('')
const targetDatasetId = ref('')
const targetDatasetName = computed(() => datasets.value.find((item) => item.id === targetDatasetId.value)?.name || '')

const pipelineForm = ref<InstanceType<typeof PipelineInputs>>()
const pipelineInputs = ref<Record<string, any>>({})
const supportedExtensions = ref<string[]>([])
const onlineExtensions = new Set(['adoc', 'axls', 'able'])
watch(targetDatasetId, async (id) => {
  pipelineInputs.value = {}
  supportedExtensions.value = []
  if (!id) return
  try {
    const response = await listSupportedExtensions(id)
    if (id === targetDatasetId.value) supportedExtensions.value = response.extensions
  } catch { /* request 拦截器提示 */ }
})
function isSyncable(row: { extension?: string | null; name?: string }) {
  const ext = (row.extension || row.name?.split('.').pop() || '').toLowerCase().replace(/^\./, '')
  return onlineExtensions.has(ext) || supportedExtensions.value.includes(ext)
}
function formatSize(bytes: number) {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
// Dify 知识库列表持久缓存 1 小时：
// 命中秒开下拉并后台校准，切「文档同步」页签不再等 Dify API
const DS_STORE_KEY = 'kge:dify_datasets_v1'
const DS_STORE_TTL = 3_600_000

async function loadDatasets() {
  try {
    const raw = localStorage.getItem(DS_STORE_KEY)
    if (raw) {
      const c = JSON.parse(raw) as { ts: number; items: DifyDataset[] }
      if (Date.now() - c.ts <= DS_STORE_TTL && c.items?.length) {
        datasets.value = c.items
        if (!datasets.value.some((item) => item.id === targetDatasetId.value)) targetDatasetId.value = datasets.value[0]?.id || ''
        listDifyDatasets().then((response) => {
          const items = response.error ? [] : response.items || []
          if (items.length) {
            datasets.value = items
            datasetsError.value = response.error || ''
            if (!datasets.value.some((item) => item.id === targetDatasetId.value)) targetDatasetId.value = datasets.value[0]?.id || ''
            try { localStorage.setItem(DS_STORE_KEY, JSON.stringify({ ts: Date.now(), items })) } catch { /* 忽略 */ }
          }
        }).catch(() => { /* 后台校准失败保留缓存 */ })
        return
      }
    }
  } catch { /* 缓存读取失败走网络 */ }
  datasetsLoading.value = true
  datasetsError.value = ''
  try {
    const response = await listDifyDatasets()
    datasets.value = response.items || []
    datasetsError.value = response.error || ''
    if (!datasets.value.some((item) => item.id === targetDatasetId.value)) targetDatasetId.value = datasets.value[0]?.id || ''
    if (datasets.value.length) {
      try { localStorage.setItem(DS_STORE_KEY, JSON.stringify({ ts: Date.now(), items: datasets.value })) } catch { /* 忽略 */ }
    }
  } catch (e: any) {
    datasets.value = []
    targetDatasetId.value = ''
    datasetsError.value = e?.response?.data?.detail || e?.message || '加载 Dify 知识库失败'
  } finally { datasetsLoading.value = false }
}

// ============ 钉钉按目录查询（知识库 + 文件夹 → 查询） ============
const CACHE_TTL = 5 * 60 * 1000 // 浏览器内存缓存 5 分钟，不持久化
// 知识库列表持久缓存 1 小时：钉钉侧接口重（1.5~3s）且列表极少变化，
// 命中秒开级联选择器，后台校准；「强制刷新」绕过
const WS_STORE_KEY = 'kge:dingtalk_workspaces_v1'
const WS_STORE_TTL = 3_600_000
const wsCache = ref<{ ts: number; items: DingTalkWorkspace[] } | null>(null)
const nodesCache = new Map<string, { ts: number; items: DingTalkNode[] }>()

const workspaces = ref<DingTalkWorkspace[]>([])
const wsLoading = ref(false)
const wsError = ref('')
const workspaceId = ref('')
const workspaceRootId = computed(() => workspaces.value.find((w) => w.id === workspaceId.value)?.root_node_id || '')

const folderCascaderKey = ref(0)
const folderCascaderRef = ref()
const folderPath = ref<string[]>([]) // 级联选中值（各层 node_id）
const folderLabels = ref<string[]>([]) // 选中各级名称，用于展示
const selectedFolderId = computed(() => folderPath.value[folderPath.value.length - 1] || '')
const selectedFolderLabel = computed(() => folderLabels.value.join(' / '))

const files = ref<DingTalkNode[]>([])
const queriedFolderId = ref('') // 当前列表对应的目录
const loading = ref(false)
const error = ref('')
const search = ref('')
const page = ref(1)
const size = ref(20)
const selectedIds = ref<string[]>([])
const syncing = ref(false)
const results = ref<{ name: string; ok: boolean; message: string }[]>([])

const canQuery = computed(() => !!(workspaceId.value && selectedFolderId.value))
const queried = computed(() => !!queriedFolderId.value)

const filteredFiles = computed(() => {
  const kw = search.value.trim().toLowerCase()
  return kw ? files.value.filter((f) => f.name.toLowerCase().includes(kw)) : files.value
})
const pageFiles = computed(() => filteredFiles.value.slice((page.value - 1) * size.value, page.value * size.value))

function readWsStore(): DingTalkWorkspace[] | null {
  try {
    const raw = localStorage.getItem(WS_STORE_KEY)
    if (!raw) return null
    const c = JSON.parse(raw) as { ts: number; items: DingTalkWorkspace[] }
    return Date.now() - c.ts <= WS_STORE_TTL && c.items?.length ? c.items : null
  } catch { return null }
}
function writeWsStore(items: DingTalkWorkspace[]) {
  try { localStorage.setItem(WS_STORE_KEY, JSON.stringify({ ts: Date.now(), items })) } catch { /* 忽略 */ }
}

async function loadWorkspaces(force = false) {
  if (!force && wsCache.value && Date.now() - wsCache.value.ts < CACHE_TTL) {
    workspaces.value = wsCache.value.items
    return
  }
  // 持久缓存命中：立即填充（秒开），后台静默校准并回写两层缓存
  if (!force) {
    const stored = readWsStore()
    if (stored) {
      workspaces.value = stored
      wsCache.value = { ts: Date.now(), items: stored }
      fetchDingTalkWorkspaces().then((res) => {
        const items = res.items || []
        if (items.length) { workspaces.value = items; wsCache.value = { ts: Date.now(), items }; writeWsStore(items) }
      }).catch(() => { /* 后台校准失败保留缓存数据 */ })
      return
    }
  }
  wsLoading.value = true
  wsError.value = ''
  try {
    const res = await fetchDingTalkWorkspaces()
    workspaces.value = res.items || []
    wsError.value = res.error || ''
    wsCache.value = { ts: Date.now(), items: workspaces.value }
    if (workspaces.value.length) writeWsStore(workspaces.value)
  } catch (e: any) {
    wsError.value = e?.response?.data?.detail || e?.message || '获取钉钉知识库失败'
  } finally { wsLoading.value = false }
}

// 文件夹级联懒加载：从知识库根节点开始，逐层拉取子目录（仅 FOLDER）
async function lazyLoadFolders(node: any, resolve: (nodes: CascaderOption[]) => void) {
  const parentId: string = node.level === 0 ? workspaceRootId.value : node.data.value
  // 级联面板随页面挂载即触发根级 lazyLoad；未选知识库（无根节点 ID）时直接返回空，不发无效请求
  if (!parentId) { resolve([]); return }
  const cached = nodesCache.get(parentId)
  let folders: DingTalkNode[]
  if (cached && Date.now() - cached.ts < CACHE_TTL) {
    folders = cached.items.filter((n) => n.is_folder)
  } else {
    try {
      const res = await fetchDingTalkNodes(parentId)
      nodesCache.set(parentId, { ts: Date.now(), items: res.items || [] })
      folders = (res.items || []).filter((n) => n.is_folder)
    } catch (e: any) {
      ElMessage.error(e?.response?.data?.detail || e?.message || '获取子目录失败')
      folders = []
    }
  }
  resolve(folders.map((f) => ({
    value: f.node_id,
    label: f.name,
    leaf: !f.has_children,
  })))
}

function changeWorkspace() {
  // 切换知识库：清空文件夹选择与列表
  folderPath.value = []
  folderLabels.value = []
  folderCascaderKey.value++ // 重建级联组件，重新从根加载
  resetFileList()
}

function handleFolderChange() {
  const nodes = folderCascaderRef.value?.getCheckedNodes(false) || []
  folderLabels.value = (nodes[0]?.pathLabels || []).map((l: any) => String(l))
  resetFileList()
}

function resetFileList() {
  files.value = []
  queriedFolderId.value = ''
  error.value = ''
  search.value = ''
  page.value = 1
  selectedIds.value = []
}

async function queryFiles(force = false) {
  if (!canQuery.value) { ElMessage.warning('请先选择钉钉知识库和文件夹'); return }
  const folderId = selectedFolderId.value
  const cached = nodesCache.get(folderId)
  if (!force && cached && Date.now() - cached.ts < CACHE_TTL) {
    files.value = cached.items.filter((n) => !n.is_folder)
    queriedFolderId.value = folderId
    page.value = 1
    selectedIds.value = []
    error.value = ''
    return
  }
  loading.value = true
  error.value = ''
  try {
    const res = await fetchDingTalkNodes(folderId)
    const items = res.items || []
    nodesCache.set(folderId, { ts: Date.now(), items })
    files.value = items.filter((n) => !n.is_folder)
    queriedFolderId.value = folderId
    page.value = 1
    selectedIds.value = []
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '查询目录内容失败'
  } finally { loading.value = false }
}

function changeSelection(rows: DingTalkNode[]) { selectedIds.value = rows.filter(isSyncable).map((row) => row.node_id) }
async function syncSelected() {
  if (!targetDatasetId.value) { ElMessage.warning('请选择 Dify 目标知识库'); return }
  if (pipelineForm.value && !pipelineForm.value.validate()) return
  const selected = files.value.filter((file) => selectedIds.value.includes(file.node_id) && isSyncable(file))
  if (!selected.length) { ElMessage.warning('请勾选可同步的钉钉文档'); return }
  syncing.value = true
  results.value = []
  for (const file of selected) {
    try {
      const res: any = await syncDingTalkFile(targetDatasetId.value, { node_id: file.node_id, name: file.name, size: file.size, pipeline_inputs: { ...pipelineInputs.value } })
      const method = res?.source_kind === 'export' ? '（钉钉官方导出）' : '（原文件）'
      results.value.push({ name: file.name, ok: true, message: `已同步至「${targetDatasetName.value}」${method}` })
    } catch (e: any) {
      results.value.push({ name: file.name, ok: false, message: e?.response?.data?.detail || e?.message || '同步失败' })
    }
  }
  syncing.value = false
  selectedIds.value = []
}

onMounted(loadWorkspaces)
// 「文档同步」页签懒加载：首次切入时拉取 Dify 知识库列表
watch(activeTab, (tab) => {
  if (tab === 'docs' && !datasets.value.length && !datasetsLoading.value) loadDatasets()
})
</script>

<template>
  <div class="kge-page">
    <el-tabs v-model="activeTab" class="sync-tabs">
      <el-tab-pane label="目录同步" name="dirs">
        <Sources />
      </el-tab-pane>
      <el-tab-pane label="文档同步" name="docs" lazy>
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <div><b>从钉钉选择文档</b><span class="hint">选择知识库下的指定文件夹后查询，仅加载该目录的文档。</span></div>
              <div class="target-actions">
                <el-select v-model="targetDatasetId" :disabled="syncing" placeholder="选择 Dify 目标知识库" :loading="datasetsLoading" style="width:260px">
                  <el-option v-for="item in datasets" :key="item.id" :label="`${item.name}（${item.document_count} 篇）`" :value="item.id" />
                </el-select>
                <el-button :loading="datasetsLoading" @click="loadDatasets">刷新知识库</el-button>
                <el-button type="primary" :loading="syncing" :disabled="!targetDatasetId || !selectedIds.length" @click="syncSelected">同步选中（{{ selectedIds.length }}）</el-button>
              </div>
            </div>
          </template>
          <el-alert v-if="datasetsError" type="warning" :closable="false" :title="datasetsError" style="margin-bottom:12px" />
          <PipelineInputs ref="pipelineForm" :dataset-id="targetDatasetId" v-model="pipelineInputs" :disabled="syncing" />
          <div class="filters">
            <el-select v-model="workspaceId" :loading="wsLoading" clearable placeholder="选择钉钉知识库" style="width:220px" @change="changeWorkspace">
              <el-option v-for="workspace in workspaces" :key="workspace.id" :label="workspace.name" :value="workspace.id" />
            </el-select>
            <el-cascader
              :key="folderCascaderKey"
              ref="folderCascaderRef"
              v-model="folderPath"
              :props="{ lazy: true, lazyLoad: lazyLoadFolders, checkStrictly: true }"
              :disabled="!workspaceId"
              placeholder="选择文件夹"
              clearable
              style="width:320px"
              @change="handleFolderChange"
            />
            <el-button type="primary" :loading="loading" :disabled="!canQuery" @click="queryFiles()">查询</el-button>
            <el-button v-if="queried" :loading="loading" @click="queryFiles(true)">强制刷新</el-button>
          </div>
          <el-alert v-if="wsError" type="warning" :closable="false" :title="wsError" style="margin-bottom:12px" />
          <el-alert v-if="error" type="error" :closable="false" :title="error" style="margin-bottom:12px" />
          <template v-if="!queried">
            <el-empty description="请先选择钉钉知识库和文件夹，点击「查询」加载该目录的文档" :image-size="90" />
          </template>
          <template v-else>
            <div class="list-meta">
              <span>目录：<b>{{ selectedFolderLabel || '/' }}</b></span>
              <span class="hint">共 {{ filteredFiles.length }} 个文档</span>
              <el-input v-model="search" clearable placeholder="过滤文件名" style="width:200px; margin-left:auto" @input="page = 1" />
            </div>
            <el-table :data="pageFiles" v-loading="loading" row-key="node_id" @selection-change="changeSelection">
              <el-table-column type="selection" width="48" :selectable="isSyncable" />
              <el-table-column prop="name" label="文档名称" min-width="300" show-overflow-tooltip><template #default="{ row }"><div class="doc-name-cell"><img class="file-ico" :src="fileIcon(row.name)" alt="" />{{ row.name }}</div></template></el-table-column>
              <el-table-column label="格式" width="90"><template #default="{ row }"><el-tag size="small" :type="isSyncable(row) ? 'success' : 'info'">{{ (row.extension || '—').toUpperCase() }}</el-tag></template></el-table-column>
              <el-table-column label="大小" width="100"><template #default="{ row }">{{ formatSize(row.size) }}</template></el-table-column>
              <el-table-column label="同步状态" width="100"><template #default="{ row }"><el-tag size="small" :type="isSyncable(row) ? 'success' : 'warning'">{{ isSyncable(row) ? '可同步' : '暂不支持' }}</el-tag></template></el-table-column>
            </el-table>
            <div class="pagination"><el-pagination v-model:current-page="page" v-model:page-size="size" :total="filteredFiles.length" :page-sizes="[20, 50, 100]" layout="total, sizes, prev, pager, next" /></div>
          </template>
          <div v-if="results.length" class="result-list"><div v-for="item in results" :key="item.name" :class="['result', item.ok ? 'success' : 'failed']">{{ item.ok ? '✓' : '×' }} <b>{{ item.name }}</b> · {{ item.message }}</div></div>
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
/* 文档名称列：类型图标 + 名称 */
.doc-name-cell { display: flex; align-items: center; gap: 8px; min-width: 0; }
.file-ico { width: 22px; height: 22px; flex: none; object-fit: contain; }
.sync-tabs :deep(.el-tabs__item) { font-size: 15px; font-weight: 600; }
.card-header { display:flex; align-items:center; justify-content:space-between; gap:16px; }
.hint { margin-left: 10px; color: var(--el-text-color-secondary); font-size: 12px; }
.target-actions, .filters { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.filters { margin-bottom:12px; }
.list-meta { display:flex; align-items:center; gap:12px; margin-bottom:10px; font-size:13px; }
.pagination { display:flex; justify-content:flex-end; margin-top:12px; }
.result-list { margin-top:16px; }
.result { padding:9px 12px; border-radius:6px; margin-top:6px; font-size:13px; }
.result.success { color:#166534; background:#f0fdf4; }
.result.failed { color:#991b1b; background:#fef2f2; }
</style>
