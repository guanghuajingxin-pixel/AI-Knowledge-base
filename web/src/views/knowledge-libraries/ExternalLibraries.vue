<script setup lang="ts">
/**
 * 知识库（检索抽象层）- RAGFlow / DIFY 知识库镜像管理
 * 仅登记 platform + dataset_id 引用（镜像），供智能体检索选库；
 * 检索策略由抽象层按平台内部决定，本页不支持导入/解析新文档。
 */
import { ref, reactive, computed, onMounted, } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Edit, Delete, Search, ArrowDown, ArrowRight, Connection, Aim, SetUp, Cpu, QuestionFilled } from '@element-plus/icons-vue'
import {
  listKnowledgeLibraries,
  createKnowledgeLibrary,
  updateKnowledgeLibrary,
  deleteKnowledgeLibrary,
  testKnowledgeRetrieval,
  type KnowledgeLibrary,
  type LibraryPlatform,
  type RetrievalTestResult,
} from '@/api/knowledge-library'
import { listDifyDatasets } from '@/api/dify'
import { listRagflowDatasets } from '@/api/ragflow'

// 页签切换：文档库（RAGFlow/DIFY 镜像登记） / 问答库（仅 kb_type=FAQ）
const props = defineProps<{ platform: LibraryPlatform }>()
const activeTab = ref('external')
// 问答库页懒加载，切到该页签时才挂载


const PLATFORM_LABEL: Record<LibraryPlatform, string> = {
  dify: 'DIFY',
  ragflow: 'RagFlow',
}
const PLATFORM_TAG: Record<LibraryPlatform, 'success' | 'warning'> = {
  dify: 'success',
  ragflow: 'warning',
}

// ===== 列表 =====
const libraries = ref<KnowledgeLibrary[]>([])
const loading = ref(false)
const filterPlatform = computed(() => props.platform)

// 引擎文档数（挂载时拉取引擎库列表做镜像映射，失败降级为 —）
const docCountMap = ref<Map<string, number>>(new Map())

const filteredLibraries = computed(() =>
  filterPlatform.value
    ? libraries.value.filter((l) => l.platform === filterPlatform.value)
    : libraries.value,
)

const stats = computed(() => ({
  total: libraries.value.length,
  dify: libraries.value.filter((l) => l.platform === 'dify').length,
  ragflow: libraries.value.filter((l) => l.platform === 'ragflow').length,
  enabled: libraries.value.filter((l) => l.enabled).length,
}))

function docCount(row: KnowledgeLibrary): number | null {
  return docCountMap.value.get(`${row.platform}:${row.dataset_id}`) ?? null
}

async function loadLibraries() {
  loading.value = true
  try {
    libraries.value = (await listKnowledgeLibraries()).filter(l => l.library_type !== 'document' && l.platform === props.platform)
  } catch (e: any) {
    ElMessage.error('加载知识库列表失败：' + (e?.message || e))
  } finally {
    loading.value = false
  }
}

async function loadDocCounts() {
  // 引擎库列表仅用于展示文档数；任一平台失败不影响另一平台
  const map = new Map<string, number>()
  try {
    const res = props.platform === 'dify' ? await listDifyDatasets() : await listRagflowDatasets()
    for (const d of res.items || []) map.set(`${props.platform}:${d.id}`, d.document_count ?? 0)
  } catch { /* Optional connector can be offline. */ }

  docCountMap.value = map
}

onMounted(() => {
  loadLibraries()
  loadDocCounts()
})

// ===== 新增 / 编辑弹窗 =====
const dialogVisible = ref(false)
const isEdit = ref(false)
const currentId = ref<number | null>(null)

const form = reactive({
  name: '',
  platform: 'ragflow' as LibraryPlatform,
  dataset_id: '',
  description: '',
  enabled: true,
})

function openAddDialog() {
  isEdit.value = false
  currentId.value = null
  Object.assign(form, {
    name: '',
    platform: filterPlatform.value || 'ragflow',
    dataset_id: '',
    description: '',
    enabled: true,
  })
  dialogVisible.value = true
}

function openEditDialog(row: KnowledgeLibrary) {
  isEdit.value = true
  currentId.value = row.id
  Object.assign(form, {
    name: row.name,
    platform: row.platform,
    dataset_id: row.dataset_id,
    description: row.description || '',
    enabled: row.enabled,
  })
  dialogVisible.value = true
}

async function handleSave() {
  if (!form.name.trim()) return ElMessage.warning('请输入知识库名称')
  if (!isEdit.value && !form.dataset_id.trim()) return ElMessage.warning('请先从平台选择知识库')
  try {
    if (isEdit.value && currentId.value) {
      await updateKnowledgeLibrary(currentId.value, { name: form.name, description: form.description })
      ElMessage.success('已保存')
    } else {
      await createKnowledgeLibrary({
        name: form.name,
        platform: form.platform,
        dataset_id: form.dataset_id,
        description: form.description,
        enabled: form.enabled,
      })
      ElMessage.success('知识库已登记')
    }
    dialogVisible.value = false
    loadLibraries()
  } catch (e: any) {
    ElMessage.error('保存失败：' + (e?.message || e))
  }
}

// ===== 启用开关（单状态文案：控件与状态同行） =====
async function handleToggle(row: KnowledgeLibrary) {
  try {
    const updated = await updateKnowledgeLibrary(row.id, { enabled: !row.enabled })
    row.enabled = updated.enabled
  } catch (e: any) {
    ElMessage.error('状态更新失败：' + (e?.message || e))
  }
}

// ===== 删除 =====
async function handleDelete(row: KnowledgeLibrary) {
  try {
    await ElMessageBox.confirm(
      `确认删除知识库「${row.name}」？仅移除镜像登记，不影响 ${PLATFORM_LABEL[row.platform]} 平台上的原知识库。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
    await deleteKnowledgeLibrary(row.id)
    ElMessage.success('已删除')
    loadLibraries()
  } catch {
    // 用户取消
  }
}

// ===== 引擎知识库选择弹窗（RAGFlow / DIFY 复用） =====
interface EngineItem { id: string; name: string; meta: string }
const pickerVisible = ref(false)
const pickerLoading = ref(false)
const pickerError = ref('')
const pickerKeyword = ref('')
const pickerItems = ref<EngineItem[]>([])
const registeredIds = ref<Set<string>>(new Set())

const pickerLabel = computed(() => PLATFORM_LABEL[form.platform])

const filteredPickerItems = computed(() => {
  const kw = pickerKeyword.value.trim().toLowerCase()
  if (!kw) return pickerItems.value
  return pickerItems.value.filter(
    (i) => i.name.toLowerCase().includes(kw) || i.id.toLowerCase().includes(kw),
  )
})

function isRegistered(item: EngineItem): boolean {
  return registeredIds.value.has(item.id)
}

async function openPicker() {
  pickerVisible.value = true
  pickerKeyword.value = ''
  pickerError.value = ''
  pickerItems.value = []
  pickerLoading.value = true
  try {
    // 已登记的同平台库置灰，避免重复添加（拉全量，不受页面筛选影响）
    const all = await listKnowledgeLibraries().catch(() => [] as KnowledgeLibrary[])
    registeredIds.value = new Set(
      all.filter((l) => l.platform === form.platform).map((l) => l.dataset_id),
    )
    if (form.platform === 'ragflow') {
      const res = await listRagflowDatasets()
      if (res.error) { pickerError.value = res.error; return }
      pickerItems.value = (res.items || []).map((d) => ({
        id: d.id, name: d.name, meta: `文档 ${d.document_count ?? 0} · 分块 ${d.chunk_count ?? 0}`,
      }))
    } else {
      const res = await listDifyDatasets()
      if (res.error) { pickerError.value = res.error; return }
      pickerItems.value = (res.items || []).map((d: any) => ({
        id: d.id, name: d.name, meta: `文档 ${d.document_count ?? 0}`,
      }))
    }
  } catch (e: any) {
    pickerError.value = e?.message || `获取 ${pickerLabel.value} 知识库失败`
  } finally {
    pickerLoading.value = false
  }
}

function pickItem(item: EngineItem) {
  if (isRegistered(item)) return
  form.name = item.name
  form.dataset_id = item.id
  pickerVisible.value = false
  ElMessage.success(`已选择「${item.name}」`)
}

function fmtTime(iso: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false })
}

// ===== 检索测试：逐库执行与智能问答一致的底层检索，排查「引擎能搜到、问答搜不到」断层 =====
// 布局参考终端检索测试台：左栏=检索词输入+最近测试，右栏=检索设置+测试结果
const testVisible = ref(false)
const testLoading = ref(false)
const testForm = reactive({
  query: '',
  libraryIds: [] as number[],
})
const testResult = ref<RetrievalTestResult | null>(null)

// ===== 检索策略设置：左栏模式 chip / 右栏「检索模式」格均可打开设置弹窗修改 =====
type RetrievalMode = 'hybrid' | 'vector' | 'fulltext'
const MODE_LABEL: Record<RetrievalMode, string> = { hybrid: '混合检索', vector: '向量检索', fulltext: '全文检索' }
const testSettings = reactive({
  mode: 'hybrid' as RetrievalMode,
  // 混合检索子策略：rerank=Rerank 模型重排 / weight=权重设置（向量权重滑条）
  hybridSub: 'rerank' as 'rerank' | 'weight',
  rerankModel: 'bge-reranker-v2-m3',
  vectorWeight: 0.3,
  topK: 8,
  scoreEnabled: true,
  scoreThreshold: 0.2,
})
const modeLabel = computed(() => MODE_LABEL[testSettings.mode])

// 设置弹窗草稿：点「保存」才落回 testSettings
const settingsVisible = ref(false)
const settingsDraft = reactive({
  mode: 'hybrid' as RetrievalMode,
  hybridSub: 'rerank' as 'rerank' | 'weight',
  rerankModel: 'bge-reranker-v2-m3',
  vectorWeight: 0.3,
  topK: 8,
  scoreEnabled: true,
  scoreThreshold: 0.2,
})

function openSettings() {
  Object.assign(settingsDraft, testSettings)
  settingsVisible.value = true
}

function saveSettings() {
  Object.assign(testSettings, settingsDraft)
  settingsVisible.value = false
}

// 知识库范围选择（弹层多选）：全选时展示「全部知识」
const scopeLabel = computed(() => {
  const total = libraries.value.length
  const n = testForm.libraryIds.length
  if (!n) return '未选知识库'
  return n >= total ? '全部知识' : `已选 ${n}/${total}`
})
const allLibsSelected = computed(
  () => libraries.value.length > 0 && testForm.libraryIds.length >= libraries.value.length,
)
function toggleAllLibs(val: any) {
  testForm.libraryIds = val ? libraries.value.map((l) => l.id) : []
}

// 最近测试记录（localStorage 仅保留 60 天，点击单条重新执行查询）
interface TestHistoryItem {
  query: string
  libraryIds: number[]
  mode: RetrievalMode
  hybridSub: 'rerank' | 'weight'
  rerankModel?: string
  topK: number
  similarityThreshold: number
  vectorWeight: number
  total: number
  ts: number
}
const TEST_HISTORY_KEY = 'kge:retrieval_test_history_v1'
const testHistory = ref<TestHistoryItem[]>([])

function loadTestHistory() {
  try {
    const arr: TestHistoryItem[] = JSON.parse(localStorage.getItem(TEST_HISTORY_KEY) || '[]')
    const minTs = Date.now() - 60 * 24 * 3600 * 1000
    testHistory.value = Array.isArray(arr) ? arr.filter((h) => h && h.ts >= minTs) : []
  } catch {
    testHistory.value = []
  }
}

function saveTestHistory() {
  try {
    localStorage.setItem(TEST_HISTORY_KEY, JSON.stringify(testHistory.value.slice(0, 30)))
  } catch {
    /* 存储失败不影响功能 */
  }
}

function fmtHistoryTime(ts: number): string {
  return new Date(ts).toLocaleString('zh-CN', { hour12: false })
}

// 历史记录行首的模式文案（旧记录无 mode 字段，默认混合/Rerank）
function modeHistoryLabel(h: { mode?: RetrievalMode; hybridSub?: 'rerank' | 'weight' }): string {
  if (h.mode === 'vector') return '向量检索'
  if (h.mode === 'fulltext') return '全文检索'
  if (h.mode === 'hybrid' && h.hybridSub === 'weight') return '混合检索/权重'
  return '混合检索/Rerank'
}

function applyHistory(h: TestHistoryItem) {
  testForm.query = h.query
  testForm.libraryIds = [...h.libraryIds]
  testSettings.mode = h.mode ?? 'hybrid'
  testSettings.hybridSub = h.hybridSub ?? 'rerank'
  if (h.rerankModel) testSettings.rerankModel = h.rerankModel
  testSettings.topK = h.topK
  testSettings.scoreThreshold = h.similarityThreshold
  testSettings.scoreEnabled = h.similarityThreshold > 0
  testSettings.vectorWeight = h.vectorWeight
  runRetrievalTest()
}

function openTestDialog() {
  if (!libraries.value.length) return ElMessage.warning('请先登记知识库')
  testResult.value = null
  // 默认全选库（含停用库，便于排查停用导致的搜不到）
  testForm.libraryIds = libraries.value.map((l) => l.id)
  testForm.query = ''
  loadTestHistory()
  testVisible.value = true
}

// 结果正文展开/收起（按命中序号记录）
const expandedContent = reactive(new Set<number>())

async function runRetrievalTest() {
  if (!testForm.query.trim()) return ElMessage.warning('请输入检索词')
  if (!testForm.libraryIds.length) return ElMessage.warning('请至少选择一个知识库')
  testLoading.value = true
  expandedContent.clear()
  try {
    const res = await testKnowledgeRetrieval({
      query: testForm.query.trim(),
      library_ids: testForm.libraryIds,
      top_k: testSettings.topK,
      mode: testSettings.mode,
      // Score 阈值开关关闭时传 0（RAGFlow 语义：不过滤）
      similarity_threshold: testSettings.scoreEnabled ? testSettings.scoreThreshold : 0,
      // 混合检索-权重设置：显式传向量权重；-Rerank：传 rerank 模型名（仅 RagFlow 生效）
      ...(testSettings.mode === 'hybrid' && testSettings.hybridSub === 'weight'
        ? { vector_similarity_weight: testSettings.vectorWeight }
        : {}),
      ...(testSettings.mode === 'hybrid' && testSettings.hybridSub === 'rerank' && testSettings.rerankModel.trim()
        ? { rerank_id: testSettings.rerankModel.trim() }
        : {}),
    })
    testResult.value = res
    // 写入最近测试（同检索词+同模式+同 TopK 去重置顶），仅本地留存
    const item: TestHistoryItem = {
      query: testForm.query.trim(),
      libraryIds: [...testForm.libraryIds],
      mode: testSettings.mode,
      hybridSub: testSettings.hybridSub,
      rerankModel: testSettings.rerankModel,
      topK: testSettings.topK,
      similarityThreshold: testSettings.scoreEnabled ? testSettings.scoreThreshold : 0,
      vectorWeight: testSettings.vectorWeight,
      total: res.total,
      ts: Date.now(),
    }
    testHistory.value = [
      item,
      ...testHistory.value.filter(
        (h) => !(h.query === item.query && h.topK === item.topK && h.similarityThreshold === item.similarityThreshold && (h.mode ?? 'hybrid') === item.mode),
      ),
    ]
    saveTestHistory()
  } catch (e: any) {
    ElMessage.error('检索测试失败：' + (e?.message || e))
  } finally {
    testLoading.value = false
  }
}

function testScoreWidth(score?: number): string {
  if (score == null) return '0%'
  return `${Math.min(100, Math.max(4, Number(score) * 100))}%`
}

function fmtElapsed(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`
}

function shortSegId(id?: string): string {
  return id ? id.slice(0, 8) : ''
}
</script>

<template>
  <div class="kl-page">
    <el-tabs v-model="activeTab" class="kl-tabs external-tabs">
      <!-- 页签一：文档库（RAGFlow / DIFY 镜像登记） -->
      <el-tab-pane :label="`${PLATFORM_LABEL[props.platform]}库`" name="external">
        <div class="pane-scroll">
    <!-- 统计卡片 -->
    <div class="kl-stats">
      <div class="stat-card">
        <div class="stat-value">{{ stats.total }}</div>
        <div class="stat-label">知识库总数</div>
      </div>
      <div class="stat-card">
        <div class="stat-value stat-dify">{{ stats.dify }}</div>
        <div class="stat-label">DIFY</div>
      </div>
      <div class="stat-card">
        <div class="stat-value stat-ragflow">{{ stats.ragflow }}</div>
        <div class="stat-label">RagFlow</div>
      </div>
      <div class="stat-card">
        <div class="stat-value stat-enabled">{{ stats.enabled }}</div>
        <div class="stat-label">已开放检索</div>
      </div>
    </div>

    <!-- 列表 -->
    <div class="kl-content">
      <div class="page-intro">
        统一登记 RAGFlow / DIFY 知识库镜像供智能体检索；检索策略由知识库层按平台自动决定，此处不支持导入或解析新文档。
      </div>
      <div class="filter-bar">
        <div class="filter-actions">
          <el-button :icon="Search" @click="openTestDialog">检索测试</el-button>
          <el-button :icon="Refresh" :loading="loading" @click="loadLibraries">刷新</el-button>
          <el-button type="primary" :icon="Plus" @click="openAddDialog">添加知识库</el-button>
        </div>
      </div>

      <el-table
        :data="filteredLibraries"
        v-loading="loading"
        stripe
        style="width: 100%"
        empty-text="暂无知识库，点击「添加知识库」从 RAGFlow / DIFY 选择登记"
      >
        <el-table-column prop="name" label="知识库名称" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="lib-name">
              <el-tag :type="PLATFORM_TAG[row.platform as LibraryPlatform]" size="small" effect="light" class="type-tag">
                {{ PLATFORM_LABEL[row.platform as LibraryPlatform] }}
              </el-tag>
              <span>{{ row.name }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="文档数" width="90" align="center">
          <template #default="{ row }">
            <span>{{ docCount(row as KnowledgeLibrary) ?? '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="desc">{{ (row as KnowledgeLibrary).description || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="dataset_id" label="数据集 ID" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="dataset-id">{{ (row as KnowledgeLibrary).dataset_id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="165">
          <template #default="{ row }">
            <span class="desc">{{ fmtTime((row as KnowledgeLibrary).updated_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="开放检索" width="130">
          <template #default="{ row }">
            <div class="switch-line">
              <el-switch
                :model-value="(row as KnowledgeLibrary).enabled"
                @change="handleToggle(row as KnowledgeLibrary)"
              />
              <span class="switch-text" :class="{ off: !(row as KnowledgeLibrary).enabled }">
                {{ (row as KnowledgeLibrary).enabled ? '已开放' : '已停用' }}
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <div class="action-btns">
              <el-button link type="primary" :icon="Edit" size="small" @click="openEditDialog(row as KnowledgeLibrary)">编辑</el-button>
              <el-button link type="danger" :icon="Delete" size="small" @click="handleDelete(row as KnowledgeLibrary)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>
        </div>
      </el-tab-pane>

    </el-tabs>

    <!-- 新增/编辑弹窗 -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑知识库' : '添加知识库'"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-form :model="form" label-width="90px" class="kl-form">
        <el-form-item label="知识库平台" required>
          <el-radio-group v-model="form.platform" :disabled="true" @change="form.dataset_id = ''">
            <el-radio-button value="ragflow">RagFlow</el-radio-button>
            <el-radio-button value="dify">DIFY</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="选择知识库" required>
          <div class="picker-row">
            <el-input
              :model-value="form.dataset_id"
              :placeholder="isEdit ? '' : '点击右侧按钮从平台选择'"
              readonly
              style="flex: 1"
            />
            <el-button :disabled="isEdit" @click="openPicker">从{{ pickerLabel }}选择</el-button>
          </div>
          <div class="form-hint">仅做镜像引用，文档与解析仍在原平台维护。</div>
        </el-form-item>
        <el-form-item label="知识库名称" required>
          <el-input v-model="form.name" placeholder="选择后自动填入，可修改" maxlength="200" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="可选，说明该知识库的用途" />
        </el-form-item>
        <el-form-item v-if="!isEdit" label="开放检索">
          <div class="switch-line">
            <el-switch v-model="form.enabled" />
            <span class="switch-text" :class="{ off: !form.enabled }">{{ form.enabled ? '已开放' : '已停用' }}</span>
          </div>
          <div class="form-hint">停用后，企业问答将无法检索该知识库</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSave">{{ isEdit ? '保存修改' : '登记知识库' }}</el-button>
      </template>
    </el-dialog>

    <!-- 引擎知识库选择弹窗（RAGFlow / DIFY 复用） -->
    <el-dialog v-model="pickerVisible" :title="`从 ${pickerLabel} 选择知识库`" width="560px" :close-on-click-modal="false">
      <el-input v-model="pickerKeyword" clearable placeholder="按名称或 ID 搜索知识库" style="margin-bottom: 12px" />
      <div
        v-loading="pickerLoading"
        style="max-height: 400px; overflow: auto; border: 1px solid var(--el-border-color); border-radius: 6px"
      >
        <div v-if="pickerError" style="padding: 16px; color: var(--el-color-danger); font-size: 13px">{{ pickerError }}</div>
        <div v-else-if="filteredPickerItems.length === 0" style="padding: 24px; text-align: center; color: var(--el-text-color-secondary)">
          {{ pickerLoading ? '加载中…' : '暂无匹配的知识库' }}
        </div>
        <div v-else>
          <div
            v-for="item in filteredPickerItems"
            :key="item.id"
            class="engine-item"
            :class="{ 'engine-item-disabled': isRegistered(item) }"
            @click="pickItem(item)"
          >
            <div class="engine-name">
              {{ item.name }}
              <el-tag v-if="isRegistered(item)" size="small" type="info" effect="light" class="reg-tag">已登记</el-tag>
              <span class="engine-meta">{{ item.meta }}</span>
            </div>
            <div class="engine-id">{{ item.id }}</div>
          </div>
        </div>
      </div>
      <template #footer>
        <el-button @click="pickerVisible = false">取消</el-button>
      </template>
    </el-dialog>

    <!-- 检索测试工作台：左=检索输入+最近测试，右=检索设置+测试结果（逐库执行与智能问答一致的底层检索） -->
    <el-dialog v-model="testVisible" title="检索测试" width="88%" top="3vh" class="rt-dialog" :close-on-click-modal="false">
      <div class="rt-wrap">
        <!-- 左栏：检索词 + 最近测试 -->
        <div class="rt-left">
          <div class="rt-input-card">
            <div class="rt-input-top">
              <el-tooltip content="点击修改检索策略" placement="top">
                <span class="rt-mode-chip rt-mode-click" @click="openSettings">
                  {{ modeLabel }}
                  <el-icon><ArrowRight /></el-icon>
                </span>
              </el-tooltip>
              <el-popover placement="bottom-end" :width="380" trigger="click">
                <template #reference>
                  <el-button link type="primary" class="rt-scope-btn">
                    {{ scopeLabel }}
                    <el-icon><ArrowDown /></el-icon>
                  </el-button>
                </template>
                <div class="rt-scope-pop">
                  <el-checkbox
                    :model-value="allLibsSelected"
                    :indeterminate="testForm.libraryIds.length > 0 && !allLibsSelected"
                    @change="toggleAllLibs"
                  >
                    全选（含已停用库，便于排查）
                  </el-checkbox>
                  <div class="rt-scope-list">
                    <el-checkbox-group v-model="testForm.libraryIds">
                      <div v-for="l in libraries" :key="l.id" class="rt-scope-item">
                        <el-checkbox :value="l.id">
                          <span class="rt-scope-name">{{ l.name }}</span>
                          <el-tag :type="PLATFORM_TAG[l.platform]" size="small" effect="light">{{ PLATFORM_LABEL[l.platform] }}</el-tag>
                          <span v-if="!l.enabled" class="rt-scope-off">已停用</span>
                        </el-checkbox>
                      </div>
                    </el-checkbox-group>
                  </div>
                </div>
              </el-popover>
            </div>
            <el-input
              v-model="testForm.query"
              type="textarea"
              :rows="7"
              :maxlength="200"
              show-word-limit
              resize="none"
              placeholder="请输入检索词，如：项目 WBS 是什么"
              class="rt-textarea"
              @keydown.enter.exact.prevent="runRetrievalTest"
            />
            <div class="rt-input-bottom">
              <span class="rt-input-hint">与智能问答同一检索链路 · Enter 键执行</span>
              <el-button type="primary" :loading="testLoading" @click="runRetrievalTest">检索</el-button>
            </div>
          </div>

          <div class="rt-history">
            <div class="rt-history-head">
              <span class="rt-history-title">最近测试</span>
              <span class="rt-history-note">仅展示最近 60 天的测试记录，点击单条记录后重新查询最新的知识</span>
            </div>
            <div v-if="!testHistory.length" class="rt-history-empty">暂无测试记录</div>
            <div
              v-for="(h, i) in testHistory"
              :key="h.ts + '-' + i"
              class="rt-history-item"
              @click="applyHistory(h)"
            >
              <span class="rt-history-mode">{{ modeHistoryLabel(h) }}</span>
              <span class="rt-history-q" :title="h.query">{{ h.query }}</span>
              <span class="rt-history-time">{{ fmtHistoryTime(h.ts) }}</span>
            </div>
          </div>
        </div>

        <!-- 右栏：检索设置 + 测试结果 -->
        <div class="rt-right">
          <div class="rt-section-title">检索设置</div>
          <div class="rt-settings-grid">
            <div class="rt-setting">
              <span class="rt-setting-label">检索模式</span>
              <span class="rt-setting-val rt-mode-link" @click="openSettings" title="点击修改检索策略">{{ modeLabel }}</span>
            </div>
            <div class="rt-setting">
              <span class="rt-setting-label">TopK</span>
              <el-input-number v-model="testSettings.topK" :min="1" :max="50" size="small" controls-position="right" style="width: 90px" />
            </div>
            <div class="rt-setting">
              <span class="rt-setting-label">Score 阈值</span>
              <el-tooltip :disabled="testSettings.scoreEnabled" content="阈值开关已关闭，在检索设置中开启" placement="top">
                <el-input-number v-model="testSettings.scoreThreshold" :min="0" :max="1" :step="0.01" :disabled="!testSettings.scoreEnabled" size="small" controls-position="right" style="width: 90px" />
              </el-tooltip>
            </div>
            <div class="rt-setting">
              <span class="rt-setting-label">向量权重</span>
              <el-tooltip :disabled="testSettings.mode === 'hybrid' && testSettings.hybridSub === 'weight'" content="仅混合检索-权重设置子策略下生效" placement="top">
                <el-input-number
                  v-model="testSettings.vectorWeight"
                  :min="0" :max="1" :step="0.05"
                  :disabled="!(testSettings.mode === 'hybrid' && testSettings.hybridSub === 'weight')"
                  size="small" controls-position="right" style="width: 90px"
                />
              </el-tooltip>
            </div>
          </div>
          <div class="rt-settings-note">Score 阈值与向量权重仅对 RagFlow 生效（默认 0.2 / 0.3，与智能问答链路一致），可与 RagFlow 自带检索测试对齐排查断层。</div>

          <div class="rt-results">
            <div class="rt-results-head">
              <span class="rt-section-title">测试结果</span>
              <span class="rt-results-note">根据知识库内容与测试文本的相似度进行排序</span>
              <span v-if="testResult" class="rt-elapsed">({{ fmtElapsed(testResult.elapsed_ms) }})</span>
            </div>

            <!-- 逐库状态：一眼看出哪个库检索失败及原因 -->
            <div v-if="testResult" class="rt-libs">
              <span
                v-for="lb in testResult.libraries"
                :key="lb.library_id"
                class="rt-lib-chip"
                :class="{ fail: !lb.ok }"
              >
                <el-tag :type="PLATFORM_TAG[lb.platform]" size="small" effect="light">{{ PLATFORM_LABEL[lb.platform] }}</el-tag>
                <span class="rt-lib-name" :title="lb.name">{{ lb.name }}</span>
                <span v-if="lb.ok" class="rt-lib-ok">命中 {{ lb.count }} 段</span>
                <el-tooltip v-else :content="lb.error" placement="top">
                  <span class="rt-lib-err">失败</span>
                </el-tooltip>
                <span v-if="!lb.enabled" class="rt-lib-off">已停用</span>
              </span>
            </div>

            <div v-if="!testResult" class="rt-empty">输入检索词并点击「检索」，查看各知识库命中分段</div>
            <div v-else-if="!testResult.hits.length" class="rt-empty">未命中任何分段：可降低 Score 阈值、更换检索词，或核对引擎侧解析状态</div>
            <template v-else>
              <div v-for="(hit, hi) in testResult.hits" :key="hi" class="rt-hit">
                <div class="rt-hit-head">
                  <span class="rt-rank">#{{ hi + 1 }}</span>
                  <span class="rt-hit-lib">{{ hit.library_name }}</span>
                  <span class="rt-hit-title" :title="hit.document_title">{{ hit.document_title || '未知文档' }}</span>
                  <div class="rt-score-wrap">
                    <div class="rt-score-bar"><div class="rt-score-fill" :style="{ width: testScoreWidth(hit.score) }"></div></div>
                    <span class="rt-score-val">{{ hit.score != null ? Number(hit.score).toFixed(4) : '-' }}</span>
                  </div>
                </div>
                <div class="rt-hit-content" :class="{ clamp: !expandedContent.has(hi) }">{{ hit.content }}</div>
                <div class="rt-hit-foot">
                  <span>字数 {{ (hit.content || '').length }}</span>
                  <span v-if="hit.page_number != null">页码 P{{ hit.page_number }}</span>
                  <span v-if="hit.segment_id">分段 {{ shortSegId(hit.segment_id) }}</span>
                  <el-button
                    v-if="(hit.content || '').length > 160"
                    link
                    type="primary"
                    size="small"
                    class="rt-expand-btn"
                    @click="expandedContent.has(hi) ? expandedContent.delete(hi) : expandedContent.add(hi)"
                  >{{ expandedContent.has(hi) ? '收起' : '展开' }}</el-button>
                </div>
              </div>
            </template>
          </div>
        </div>
      </div>
    </el-dialog>

    <!-- 检索设置弹窗：选择检索策略（混合/向量/全文），混合检索展开权重/Rerank 子设置 -->
    <el-dialog v-model="settingsVisible" title="检索设置" width="640px" append-to-body :close-on-click-modal="false" class="rs-dialog">
      <div class="rs-body">
        <!-- 模式一：混合检索（选中时卡片内展开子设置面板，与引擎检索设置一致） -->
        <div class="rs-card wrap" :class="{ selected: settingsDraft.mode === 'hybrid' }" @click="settingsDraft.mode = 'hybrid'">
          <div class="rs-card-main">
            <div class="rs-icon"><el-icon><Connection /></el-icon></div>
            <div class="rs-card-text">
              <div class="rs-card-title">混合检索</div>
              <div class="rs-card-desc">同时使用向量检索和全文检索两种策略进行召回，推荐在需要对句子理解和语义关联性的场景使用，综合效果更优</div>
            </div>
          </div>
          <span class="rs-radio" :class="{ on: settingsDraft.mode === 'hybrid' }"></span>

          <el-collapse-transition>
            <div v-if="settingsDraft.mode === 'hybrid'" class="rs-sub-panel" @click.stop>
              <div class="rs-sub-grid">
                <!-- 子策略一：权重设置 -->
                <div class="rs-sub-card" :class="{ selected: settingsDraft.hybridSub === 'weight' }" @click="settingsDraft.hybridSub = 'weight'">
                  <div class="rs-card-main">
                    <div class="rs-icon sm"><el-icon><SetUp /></el-icon></div>
                    <div class="rs-card-text">
                      <div class="rs-card-title sm">权重设置</div>
                      <div class="rs-card-desc">通过调整分配的权重，重新排序策略确定是优先进行语义匹配还是关键字匹配</div>
                    </div>
                  </div>
                  <span class="rs-radio sm" :class="{ on: settingsDraft.hybridSub === 'weight' }"></span>
                </div>
                <!-- 子策略二：Rerank 模型 -->
                <div class="rs-sub-card" :class="{ selected: settingsDraft.hybridSub === 'rerank' }" @click="settingsDraft.hybridSub = 'rerank'">
                  <div class="rs-card-main">
                    <div class="rs-icon sm"><el-icon><Cpu /></el-icon></div>
                    <div class="rs-card-text">
                      <div class="rs-card-title sm">Rerank 模型</div>
                      <div class="rs-card-desc">根据候选文档列表与用户问题语义匹配度进行重新排序，从而改进语义排序结果</div>
                    </div>
                  </div>
                  <span class="rs-radio sm" :class="{ on: settingsDraft.hybridSub === 'rerank' }"></span>
                </div>
              </div>

              <!-- Rerank 模型下拉（选择 Rerank 子策略时显示） -->
              <el-select
                v-if="settingsDraft.hybridSub === 'rerank'"
                v-model="settingsDraft.rerankModel"
                filterable
                allow-create
                default-first-option
                placeholder="选择或输入 Rerank 模型名"
                class="rs-rerank-select"
              >
                <el-option value="bge-reranker-v2-m3" label="bge-reranker-v2-m3" />
              </el-select>

              <!-- 向量权重滑条（选择权重设置子策略时显示） -->
              <div v-if="settingsDraft.hybridSub === 'weight'" class="rs-param-row">
                <span class="rs-param-label">向量权重</span>
                <el-slider v-model="settingsDraft.vectorWeight" :min="0" :max="1" :step="0.05" class="rs-slider" />
                <span class="rs-param-val">{{ Number(settingsDraft.vectorWeight).toFixed(2) }}</span>
              </div>

              <!-- Top K / Score 阈值：同行左右两栏 -->
              <div class="rs-param-grid">
                <div class="rs-param-row">
                  <span class="rs-param-label">
                    Top K
                    <el-tooltip content="召回段落数量" placement="top">
                      <el-icon class="rs-q"><QuestionFilled /></el-icon>
                    </el-tooltip>
                  </span>
                  <el-input-number v-model="settingsDraft.topK" :min="1" :max="50" size="small" controls-position="right" style="width: 96px" />
                  <el-slider v-model="settingsDraft.topK" :min="1" :max="50" :step="1" class="rs-slider" />
                </div>
                <div class="rs-param-row">
                  <el-switch v-model="settingsDraft.scoreEnabled" size="small" />
                  <span class="rs-param-label auto">
                    Score 阈值
                    <el-tooltip content="低于该相似度分数的分段将被过滤" placement="top">
                      <el-icon class="rs-q"><QuestionFilled /></el-icon>
                    </el-tooltip>
                  </span>
                  <el-input-number v-model="settingsDraft.scoreThreshold" :min="0" :max="1" :step="0.01" :disabled="!settingsDraft.scoreEnabled" size="small" controls-position="right" style="width: 96px" />
                  <el-slider v-model="settingsDraft.scoreThreshold" :min="0" :max="1" :step="0.01" :disabled="!settingsDraft.scoreEnabled" class="rs-slider" />
                </div>
              </div>
            </div>
          </el-collapse-transition>
        </div>

        <!-- 模式二：向量检索 -->
        <div class="rs-card" :class="{ selected: settingsDraft.mode === 'vector' }" @click="settingsDraft.mode = 'vector'">
          <div class="rs-card-main">
            <div class="rs-icon"><el-icon><Aim /></el-icon></div>
            <div class="rs-card-text">
              <div class="rs-card-title">向量检索</div>
              <div class="rs-card-desc">返回与查询 Query 含义相匹配的文本分段，而不是与查询字面意思相匹配的内容。推荐在需要对意图相关性的场景使用</div>
            </div>
          </div>
          <span class="rs-radio" :class="{ on: settingsDraft.mode === 'vector' }"></span>
        </div>

        <!-- 模式三：全文检索 -->
        <div class="rs-card" :class="{ selected: settingsDraft.mode === 'fulltext' }" @click="settingsDraft.mode = 'fulltext'">
          <div class="rs-card-main">
            <div class="rs-icon"><el-icon><Search /></el-icon></div>
            <div class="rs-card-text">
              <div class="rs-card-title">全文检索</div>
              <div class="rs-card-desc">索引文档中的所有词汇，并返回包含这些词汇的文本分段。推荐在需要对关键词精确匹配的场景下使用</div>
            </div>
          </div>
          <span class="rs-radio" :class="{ on: settingsDraft.mode === 'fulltext' }"></span>
        </div>
      </div>
      <template #footer>
        <el-button @click="settingsVisible = false">取消</el-button>
        <el-button type="primary" @click="saveSettings">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.external-tabs > :deep(.el-tabs__header) { display: none; }
.kl-page {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* 页签容器：头部固定，内容区撑满并各自滚动 */
.kl-tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.kl-tabs :deep(.el-tabs__header) {
  margin: 0;
  padding: 8px 20px 0;
  flex-shrink: 0;
}

.kl-tabs :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.kl-tabs :deep(.el-tab-pane) {
  height: 100%;
}

/* 引擎镜像页签内部滚动容器（补齐原 .kl-page 的内边距） */
.pane-scroll {
  height: 100%;
  overflow: auto;
  padding: 16px 20px 20px;
}

/* 统计卡片 */
.kl-stats {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
  flex-shrink: 0;
}

.stat-card {
  flex: 1;
  background: #fff;
  border-radius: 8px;
  padding: 14px 18px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}

.stat-value {
  font-size: 24px;
  font-weight: 700;
  color: #303133;
}

.stat-dify { color: #67c23a; }
.stat-ragflow { color: #f56c6c; }
.stat-enabled { color: var(--app-brand-blue); }

.stat-label {
  font-size: 12px;
  color: #999;
  margin-top: 4px;
}

/* 列表 */
.kl-content {
  flex: 1;
  background: #fff;
  border-radius: 8px;
  padding: 16px;
  overflow: auto;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}

.page-intro {
  font-size: 13px;
  color: #909399;
  margin-bottom: 12px;
}

.filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.filter-label {
  font-size: 13px;
  color: #666;
}

.filter-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
}

.filter-actions .el-button + .el-button {
  margin-left: 0;
}

.lib-name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 500;
}

.type-tag {
  flex-shrink: 0;
}

.desc {
  color: #999;
  font-size: 13px;
}

.dataset-id {
  font-family: 'Menlo', 'Consolas', monospace;
  font-size: 12px;
  color: #666;
  background: #f5f7fa;
  padding: 2px 6px;
  border-radius: 4px;
}

.switch-line {
  display: flex;
  align-items: center;
  gap: 8px;
}

.switch-text {
  font-size: 12px;
  color: var(--el-color-success);
}

.switch-text.off {
  color: var(--el-color-info);
}

.action-btns {
  display: flex;
  gap: 4px;
}

/* 表单 */
.kl-form {
  padding: 0 12px;
}

.picker-row {
  display: flex;
  gap: 8px;
  width: 100%;
}

.form-hint {
  font-size: 12px;
  color: #999;
  margin-top: 4px;
}

/* 引擎知识库选择列表 */
.engine-item {
  padding: 10px 14px;
  cursor: pointer;
  border-bottom: 1px solid var(--el-border-color-lighter);
  transition: background 0.15s;
}

.engine-item:hover {
  background: var(--el-color-primary-light-9);
}

.engine-item:last-child {
  border-bottom: none;
}

/* 已登记的知识库置灰不可选 */
.engine-item-disabled {
  cursor: not-allowed;
  background: #fafafa;
}

.engine-item-disabled:hover {
  background: #fafafa;
}

.engine-item-disabled .engine-name,
.engine-item-disabled .engine-id {
  color: var(--el-text-color-placeholder);
}

.engine-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--el-text-color-primary);
}

.engine-id {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  font-family: 'Menlo', 'Consolas', monospace;
  margin-top: 2px;
}

.engine-meta {
  margin-left: 8px;
  font-size: 12px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
}

.reg-tag {
  margin-left: 6px;
  flex-shrink: 0;
}

/* ===== 检索测试工作台（左右两栏：左输入+历史，右设置+结果） ===== */
.rt-wrap {
  display: flex;
  gap: 20px;
  height: 74vh;
  min-height: 520px;
}

.rt-left {
  width: 40%;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-height: 0;
}

.rt-input-card {
  border: 1px solid var(--el-border-color);
  border-radius: 10px;
  padding: 10px 12px 12px;
  transition: border-color 0.2s;
}

.rt-input-card:focus-within {
  border-color: var(--el-color-primary);
}

.rt-input-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}

.rt-mode-chip {
  font-size: 13px;
  color: #303133;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  padding: 3px 10px;
  background: #fafbfc;
}

.rt-scope-btn {
  font-size: 13px;
}

.rt-textarea :deep(.el-textarea__inner) {
  box-shadow: none;
  padding: 4px 2px;
}

.rt-input-bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 6px;
}

.rt-input-hint {
  font-size: 12px;
  color: #c0c4cc;
}

/* 弹层内的知识库范围多选 */
.rt-scope-list {
  max-height: 300px;
  overflow: auto;
  border-top: 1px solid var(--el-border-color-lighter);
  margin-top: 6px;
  padding-top: 6px;
}

.rt-scope-item {
  padding: 2px 0;
}

.rt-scope-name {
  margin-right: 6px;
}

.rt-scope-off {
  font-size: 12px;
  color: var(--el-color-info);
  margin-left: 6px;
}

/* 最近测试 */
.rt-history {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.rt-history-head {
  margin-bottom: 4px;
}

.rt-history-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
  margin-right: 8px;
}

.rt-history-note {
  font-size: 12px;
  color: #c0c4cc;
}

.rt-history-empty {
  font-size: 13px;
  color: #c0c4cc;
  padding: 24px 0;
  text-align: center;
}

.rt-history-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 6px;
  border-bottom: 1px solid #f2f4f7;
  border-radius: 6px;
  cursor: pointer;
}

.rt-history-item:hover {
  background: #f7f9fc;
}

.rt-history-mode {
  font-size: 12px;
  color: #909399;
  flex-shrink: 0;
}

.rt-history-q {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rt-history-time {
  font-size: 12px;
  color: #c0c4cc;
  flex-shrink: 0;
}

/* 右栏：设置 + 结果 */
.rt-right {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.rt-section-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
}

.rt-settings-grid {
  display: grid;
  grid-template-columns: 1.3fr 1fr 1fr 1fr;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  overflow: hidden;
  margin-top: 10px;
  flex-shrink: 0;
}

.rt-setting {
  padding: 8px 12px;
  border-left: 1px solid #ebeef5;
  background: #fafbfc;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.rt-setting:first-child {
  border-left: none;
}

.rt-setting-label {
  font-size: 12px;
  color: #909399;
}

.rt-setting-val {
  font-size: 13px;
  color: #303133;
  line-height: 24px;
}

.rt-settings-note {
  font-size: 12px;
  color: #c0c4cc;
  margin-top: 6px;
  flex-shrink: 0;
}

.rt-results {
  flex: 1;
  min-height: 0;
  overflow: auto;
  margin-top: 16px;
}

.rt-results-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  position: sticky;
  top: 0;
  background: #fff;
  padding: 4px 0;
  z-index: 1;
}

.rt-results-note {
  font-size: 12px;
  color: #c0c4cc;
}

.rt-elapsed {
  margin-left: auto;
  font-size: 12px;
  color: #909399;
  font-variant-numeric: tabular-nums;
}

/* 逐库状态 chips */
.rt-libs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 10px 0 4px;
}

.rt-lib-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid #ebeef5;
  border-radius: 16px;
  padding: 3px 10px;
  font-size: 12px;
  background: #fafbfc;
  max-width: 100%;
}

.rt-lib-chip.fail {
  border-color: #fbc4c4;
  background: #fef0f0;
}

.rt-lib-name {
  font-weight: 500;
  color: #303133;
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rt-lib-ok {
  color: var(--el-color-success);
}

.rt-lib-err {
  color: var(--el-color-danger);
  cursor: help;
  text-decoration: underline dotted;
}

.rt-lib-off {
  color: var(--el-color-info);
}

.rt-empty {
  font-size: 13px;
  color: #c0c4cc;
  padding: 40px 0;
  text-align: center;
}

/* 命中分段卡片 */
.rt-hit {
  border: 1px solid #eef0f3;
  border-radius: 10px;
  padding: 12px 16px;
  margin-top: 10px;
  background: #fff;
}

.rt-hit-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.rt-rank {
  background: var(--el-color-primary);
  color: #fff;
  font-weight: 700;
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 6px;
  flex-shrink: 0;
}

.rt-hit-lib {
  font-size: 12px;
  color: #909399;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 1px 6px;
  flex-shrink: 0;
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rt-hit-title {
  flex: 1;
  min-width: 0;
  font-size: 13.5px;
  font-weight: 600;
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rt-score-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.rt-score-bar {
  width: 72px;
  height: 6px;
  border-radius: 3px;
  background: #eef0f3;
  overflow: hidden;
}

.rt-score-fill {
  height: 100%;
  border-radius: 3px;
  background: var(--el-color-primary);
}

.rt-score-val {
  font-size: 12px;
  color: #606266;
  font-variant-numeric: tabular-nums;
  min-width: 52px;
  text-align: right;
}

.rt-hit-content {
  font-size: 13px;
  color: #606266;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-all;
}

.rt-hit-content.clamp {
  display: -webkit-box;
  -webkit-line-clamp: 5;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.rt-hit-foot {
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 12px;
  color: #909399;
  margin-top: 8px;
}

.rt-expand-btn {
  margin-left: auto;
  padding: 0;
}

/* 模式 chip / 右栏模式文字可点击 */
.rt-mode-click {
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.rt-mode-click:hover {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary);
}

.rt-mode-link {
  color: var(--el-color-primary);
  cursor: pointer;
}

.rt-mode-link:hover {
  text-decoration: underline;
}

/* ===== 检索设置弹窗（检索模式选择，参考终端检索测试台） ===== */
.rs-body {
  max-height: 62vh;
  overflow: auto;
  padding: 2px;
}

.rs-card {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 12px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}

.rs-card:hover {
  border-color: var(--el-color-primary-light-5);
}

/* 混合检索卡片：允许子设置面板换行嵌入卡片内部 */
.rs-card.wrap {
  flex-wrap: wrap;
}

.rs-card.selected {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.rs-card-main {
  display: flex;
  gap: 12px;
  flex: 1;
  min-width: 0;
}

.rs-icon {
  width: 34px;
  height: 34px;
  border-radius: 8px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #e8f3ff;
  color: var(--el-color-primary);
  font-size: 18px;
}

.rs-icon.sm {
  width: 28px;
  height: 28px;
  font-size: 14px;
}

.rs-card-title {
  font-size: 14.5px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 2px;
}

.rs-card-title.sm {
  font-size: 13.5px;
}

.rs-card-desc {
  font-size: 12.5px;
  color: #909399;
  line-height: 1.55;
}

/* 自绘 radio 圆点（右上角） */
.rs-radio {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 1.5px solid #c0c6d1;
  flex-shrink: 0;
  margin-top: 2px;
  position: relative;
  background: #fff;
}

.rs-radio.on {
  border-color: var(--el-color-primary);
}

.rs-radio.on::after {
  content: '';
  position: absolute;
  inset: 3px;
  border-radius: 50%;
  background: var(--el-color-primary);
}

.rs-radio.sm {
  width: 14px;
  height: 14px;
}

.rs-radio.sm.on::after {
  inset: 2.5px;
}

/* 混合检索展开的子设置面板（嵌入选中卡片内部，占满整行） */
.rs-sub-panel {
  flex-basis: 100%;
  background: #fff;
  border: 1px solid #e3edfd;
  border-radius: 10px;
  padding: 12px;
  margin: 2px 0 0;
}

.rs-sub-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-bottom: 10px;
}

.rs-sub-card {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  background: #fff;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 10px 12px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}

.rs-sub-card:hover {
  border-color: var(--el-color-primary-light-5);
}

.rs-sub-card.selected {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.rs-rerank-select {
  width: 100%;
  margin-bottom: 10px;
}

.rs-param-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 2px;
  background: transparent;
}

.rs-param-row + .rs-param-row {
  margin-top: 6px;
}

/* Top K / Score 阈值：同行左右两栏（与引擎检索设置弹窗一致） */
.rs-param-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 16px;
  margin-top: 6px;
}

.rs-param-grid .rs-param-row + .rs-param-row {
  margin-top: 0;
}

.rs-param-label {
  font-size: 13px;
  color: #303133;
  width: 92px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.rs-param-label.auto {
  width: auto;
}

.rs-param-val {
  font-size: 12px;
  color: #606266;
  width: 36px;
  text-align: right;
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}

.rs-slider {
  flex: 1;
  padding: 0 4px;
}

.rs-q {
  color: #c0c4cc;
  font-size: 13px;
  cursor: help;
}
</style>
