<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import PageContainer from '@/components/common/PageContainer.vue'
import ImageComponentsPanel from '@/components/library/ImageComponentsPanel.vue'
import {
  getSettings, setSetting, testLLM, listLlmModels, testDify, testMineru, testDingtalk,
  testEmbedding, testRerank,
  type SettingsResponse, type SettingItem,
  type TestLLMResult, type TestDifyResult, type TestMinerUResult, type TestDingtalkResult,
  type TestEmbeddingResult, type TestRerankResult,
  getDingtalkBotStatus, type DingtalkBotStatus,
} from '@/api/settings'
import { listDifyProfiles, createDifyProfile, updateDifyProfile, deleteDifyProfile, enableDifyProfile, type DifyProfile } from '@/api/settings'
import {
  listLlmProfiles, createLlmProfile, updateLlmProfile, deleteLlmProfile, refreshLlmProfileModels,
  type LlmProfile, type LlmModelEntry,
} from '@/api/settings'
import {
  listRerankProfiles, createRerankProfile, updateRerankProfile, deleteRerankProfile, enableRerankProfile,
  type RerankProfile,
  listRagflowProfiles, createRagflowProfile, updateRagflowProfile, deleteRagflowProfile, enableRagflowProfile,
  type RagflowProfile,
  listEmbeddingProfiles, createEmbeddingProfile, updateEmbeddingProfile, deleteEmbeddingProfile, enableEmbeddingProfile,
  type EmbeddingProfile,
  fetchExternalModels,
} from '@/api/settings'
import { testRagflow, type TestRagflowResult } from '@/api/ragflow'
import { getMenuVisibility, setMenuVisibility } from '@/api/settings'
import { useRouter } from 'vue-router'

// ============ 接入配置（功能性） ============
const EMPTY: SettingItem = { label: '', value: '', is_set: false, is_secret: false }
const form = ref<SettingsResponse>({
  site_name: { ...EMPTY },
  site_logo: { ...EMPTY },
  llm_base_url: { ...EMPTY },
  llm_api_key: { ...EMPTY, is_secret: true },
  llm_model: { ...EMPTY },
  mineru_api_key: { ...EMPTY, is_secret: true },
  dify_base_url: { ...EMPTY },
  dify_api_key: { ...EMPTY, is_secret: true },
  dify_upload_max_mb: { ...EMPTY, value: '15' },
  ragflow_base_url: { ...EMPTY },
  ragflow_api_key: { ...EMPTY, is_secret: true },
  embedding_base_url: { ...EMPTY },
  embedding_api_key: { ...EMPTY, is_secret: true },
  embedding_model: { ...EMPTY },
  rerank_api_url: { ...EMPTY },
  rerank_api_key: { ...EMPTY, is_secret: true },
  rerank_model: { ...EMPTY },
  dingtalk_app_key: { ...EMPTY },
  dingtalk_app_secret: { ...EMPTY, is_secret: true },
  dingtalk_operator_union_id: { ...EMPTY },
})
const saving = ref<string | null>(null)
const activeTab = ref('access')

// ============ LLM 供应商配置（多供应商 × 多模型列表管理） ============
const llmProviderPresets = [
  { key: 'deepseek', name: 'DeepSeek', base: 'https://api.deepseek.com/v1', doc: 'https://platform.deepseek.com/api_keys' },
  { key: 'zhipu', name: 'GLM（智谱）', base: 'https://open.bigmodel.cn/api/paas/v4', doc: 'https://open.bigmodel.cn/usercenter/apikeys' },
  { key: 'custom', name: '自定义（OpenAI 兼容）', base: '', doc: '' },
]

function providerLabel(key: string) {
  return llmProviderPresets.find((p) => p.key === key)?.name || key
}

const llmProfiles = ref<LlmProfile[]>([])
const llmLoading = ref(false)

async function loadLlmProfiles() {
  llmLoading.value = true
  try {
    llmProfiles.value = await listLlmProfiles()
  } finally {
    llmLoading.value = false
  }
}

const llmDlg = ref({
  visible: false,
  editingId: '',
  name: '',
  provider: 'deepseek',
  base_url: '',
  api_key: '',
  has_key: false,
  models: [] as LlmModelEntry[],
  fetching: false,
  testing: false,
  saving: false,
  testResult: null as TestLLMResult | null,
})

const llmDocLink = computed(() => llmProviderPresets.find((p) => p.key === llmDlg.value.provider)?.doc || '')

function openLlmCreate() {
  llmDlg.value = {
    visible: true, editingId: '', name: '', provider: 'deepseek',
    base_url: llmProviderPresets[0].base, api_key: '', has_key: false, models: [],
    fetching: false, testing: false, saving: false, testResult: null,
  }
}

function openLlmEdit(p: LlmProfile) {
  const preset = llmProviderPresets.find((x) => x.key === p.provider)
  llmDlg.value = {
    visible: true, editingId: p.id, name: p.name, provider: p.provider,
    base_url: p.base_url || preset?.base || '', api_key: '', has_key: p.has_key,
    models: p.models.map((m) => ({ ...m })),
    fetching: false, testing: false, saving: false, testResult: null,
  }
}

function onProviderChange(key: string) {
  const preset = llmProviderPresets.find((x) => x.key === key)
  if (preset && !llmDlg.value.editingId) {
    llmDlg.value.base_url = preset.base
    if (!llmDlg.value.name.trim()) llmDlg.value.name = preset.name
  }
}

// 拉取模型：编辑态且未填新 Key → 用已存 Key 调 refresh；否则用弹窗内 Key 实时拉取
async function fetchLlmModels() {
  const d = llmDlg.value
  if (!d.base_url.trim()) {
    ElMessage.warning('请先填写 API Base URL')
    return
  }
  d.fetching = true
  try {
    const newKey = d.api_key.trim()
    if (d.editingId && !newKey) {
      const res = await refreshLlmProfileModels(d.editingId)
      d.models = res.models || []
      await loadLlmProfiles()
    } else {
      const res = await listLlmModels({ base_url: d.base_url, api_key: newKey })
      const existing = new Map(d.models.map((m) => [m.name, m]))
      d.models = (res.models || []).map((name) => existing.get(name) || { name, enabled: false, is_default: false })
    }
  } catch (e: any) {
    ElMessage.error(e?.message || '拉取模型失败')
  } finally {
    d.fetching = false
  }
}

async function testLlmDlg() {
  const d = llmDlg.value
  d.testResult = null
  d.testing = true
  try {
    d.testResult = await testLLM({
      base_url: d.base_url,
      api_key: d.api_key,
      // 默认模型优先，其次第一个生效模型；均留空时后端回退该 profile 已保存值
      model: d.models.find((m) => m.is_default && m.enabled)?.name || d.models.find((m) => m.enabled)?.name || '',
      profile_id: d.editingId,
    })
  } catch (e: any) {
    d.testResult = { ok: false, message: e?.message || '请求失败' }
  } finally {
    d.testing = false
  }
}

// 默认开关互斥（本配置内）；默认模型必须生效；跨配置互斥由后端保证
function onDefaultToggle(m: LlmModelEntry) {
  if (m.is_default) {
    for (const x of llmDlg.value.models) if (x !== m) x.is_default = false
    m.enabled = true
  }
}

async function saveLlmProfile() {
  const d = llmDlg.value
  if (!d.name.trim()) return ElMessage.warning('请填写配置名称')
  if (!d.base_url.trim()) return ElMessage.warning('请填写 API Base URL')
  if (!d.editingId && !d.api_key.trim()) return ElMessage.warning('请填写 API Key')
  if (!d.models.some((m) => m.enabled)) return ElMessage.warning('请至少勾选一个生效模型')
  d.saving = true
  try {
    const payload = {
      name: d.name.trim(),
      provider: d.provider,
      base_url: d.base_url.trim(),
      api_key: d.api_key.trim(),
      models: d.models,
    }
    if (d.editingId) await updateLlmProfile(d.editingId, payload)
    else await createLlmProfile(payload)
    ElMessage.success('模型配置已保存')
    d.visible = false
    await loadLlmProfiles()
  } finally {
    d.saving = false
  }
}

async function removeLlmProfile(p: LlmProfile) {
  await ElMessageBox.confirm(`确定删除模型配置「${p.name}」？该配置下的模型选项将同步移除。`, '删除确认', { type: 'warning' })
  await deleteLlmProfile(p.id)
  ElMessage.success('已删除')
  await loadLlmProfiles()
}

// ============ RAGFlow 知识库多配置（多环境切换，多条只能生效一条） ============
const ragflowProfiles = ref<RagflowProfile[]>([])
const ragflowLoading = ref(false)

const rfDlg = ref({
  visible: false,
  editingId: '',
  name: '',
  base_url: '',
  api_key: '',
  has_key: false,
  testing: false,
  saving: false,
  testResult: null as TestRagflowResult | null,
})

async function loadRagflowProfiles() {
  ragflowLoading.value = true
  try {
    ragflowProfiles.value = await listRagflowProfiles()
  } finally {
    ragflowLoading.value = false
  }
}

function openRfCreate() {
  rfDlg.value = {
    visible: true, editingId: '', name: '', base_url: '',
    api_key: '', has_key: false,
    testing: false, saving: false, testResult: null,
  }
}

function openRfEdit(p: RagflowProfile) {
  rfDlg.value = {
    visible: true, editingId: p.id, name: p.name, base_url: p.base_url,
    api_key: '', has_key: p.has_key,
    testing: false, saving: false, testResult: null,
  }
}

async function runRfTest() {
  const d = rfDlg.value
  if (!d.base_url.trim()) {
    ElMessage.warning('请先填写服务地址')
    return
  }
  d.testResult = null
  d.testing = true
  try {
    d.testResult = await testRagflow({
      base_url: d.base_url,
      api_key: d.api_key,
      profile_id: d.editingId,
    })
  } catch (e: any) {
    d.testResult = { ok: false, message: e?.message || '请求失败' }
  } finally {
    d.testing = false
  }
}

async function saveRfProfile() {
  const d = rfDlg.value
  if (!d.name.trim()) return ElMessage.warning('请填写配置名称')
  if (!d.base_url.trim()) return ElMessage.warning('请填写服务地址')
  if (!d.editingId && !d.api_key.trim()) return ElMessage.warning('请填写 API Key（RAGFlow 接口均需认证）')
  d.saving = true
  try {
    // 编辑态：留空且原有 Key → 不修改
    const payload = { name: d.name.trim(), base_url: d.base_url.trim(), api_key: d.api_key }
    if (d.editingId) {
      await updateRagflowProfile(d.editingId, payload)
      ElMessage.success('RAGFlow 配置已更新')
    } else {
      await createRagflowProfile(payload)
      ElMessage.success('RAGFlow 配置已创建，点击「生效」切换当前使用的环境')
    }
    d.visible = false
    await loadRagflowProfiles()
  } finally {
    d.saving = false
  }
}

async function removeRfProfile(p: RagflowProfile) {
  await ElMessageBox.confirm(`确定删除 RAGFlow 配置「${p.name}」？`, '删除确认', { type: 'warning' })
  await deleteRagflowProfile(p.id)
  ElMessage.success('已删除')
  await loadRagflowProfiles()
}

async function enableRfProfile(p: RagflowProfile) {
  await enableRagflowProfile(p.id)
  ElMessage.success(`已切换生效：${p.name}`)
  await loadRagflowProfiles()
}

// ============ Rerank 重排模型多配置（多条只能生效一条） ============
const rerankProfiles = ref<RerankProfile[]>([])
const rerankLoading = ref(false)

const rrDlg = ref({
  visible: false,
  editingId: '',
  name: '',
  api_url: '',
  api_key: '',
  has_key: false,
  model: '',
  models: [] as string[],        // 拉取的候选模型列表
  fetching: false,
  testing: false,
  saving: false,
  testResult: null as TestRerankResult | null,
})

async function loadRerankProfiles() {
  rerankLoading.value = true
  try {
    rerankProfiles.value = await listRerankProfiles()
  } finally {
    rerankLoading.value = false
  }
}

function openRrCreate() {
  rrDlg.value = {
    visible: true, editingId: '', name: '', api_url: '',
    api_key: '', has_key: false, model: '', models: [],
    fetching: false, testing: false, saving: false, testResult: null,
  }
}

function openRrEdit(p: RerankProfile) {
  rrDlg.value = {
    visible: true, editingId: p.id, name: p.name, api_url: p.api_url,
    api_key: '', has_key: p.has_key, model: p.model, models: [],
    fetching: false, testing: false, saving: false, testResult: null,
  }
}

async function runRrTest() {
  const d = rrDlg.value
  if (!d.api_url.trim() || !d.model.trim()) {
    ElMessage.warning('请先填写服务地址与模型名')
    return
  }
  d.testResult = null
  d.testing = true
  try {
    d.testResult = await testRerank({
      api_url: d.api_url,
      api_key: d.api_key,
      model: d.model,
      profile_id: d.editingId,
    })
  } catch (e: any) {
    d.testResult = { ok: false, message: e?.message || '请求失败' }
  } finally {
    d.testing = false
  }
}

async function saveRrProfile() {
  const d = rrDlg.value
  if (!d.name.trim()) return ElMessage.warning('请填写配置名称')
  if (!d.api_url.trim()) return ElMessage.warning('请填写服务地址')
  if (!d.model.trim()) return ElMessage.warning('请填写模型名')
  d.saving = true
  try {
    // 编辑态：留空且原有 Key → 不修改；填 "__CLEAR__" → 清空 Key（免鉴权服务）
    let key = d.api_key
    if (d.editingId && !key && d.has_key) key = ''
    const payload = { name: d.name.trim(), api_url: d.api_url.trim(), api_key: key, model: d.model.trim() }
    if (d.editingId) {
      await updateRerankProfile(d.editingId, payload)
      ElMessage.success('重排配置已更新')
    } else {
      await createRerankProfile(payload)
      ElMessage.success('重排配置已创建，点击「生效」切换当前使用的模型')
    }
    d.visible = false
    await loadRerankProfiles()
  } finally {
    d.saving = false
  }
}

async function removeRrProfile(p: RerankProfile) {
  await ElMessageBox.confirm(`确定删除重排配置「${p.name}」？`, '删除确认', { type: 'warning' })
  await deleteRerankProfile(p.id)
  ElMessage.success('已删除')
  await loadRerankProfiles()
}

async function enableRrProfile(p: RerankProfile, enable: boolean) {
  if (!enable) {
    try {
      await ElMessageBox.confirm(
        `关闭后所有知识库检索将不再进行 Rerank 重排（可随时重新开启），确定关闭「${p.name}」？`,
        '关闭默认重排模型', { type: 'warning', confirmButtonText: '关闭', cancelButtonText: '取消' })
    } catch { return }
  }
  await enableRerankProfile(p.id, enable)
  ElMessage.success(enable ? `已设为生效模型：${p.name}` : '已关闭重排模型')
  await loadRerankProfiles()
}

// ============ Embedding 向量模型多配置（多条只能生效一条） ============
const embeddingProfiles = ref<EmbeddingProfile[]>([])
const embeddingLoading = ref(false)

const emDlg = ref({
  visible: false,
  editingId: '',
  name: '',
  api_url: '',
  api_key: '',
  has_key: false,
  model: '',
  models: [] as string[],        // 拉取的候选模型列表
  fetching: false,
  testing: false,
  saving: false,
  testResult: null as TestEmbeddingResult | null,
})

async function loadEmbeddingProfiles() {
  embeddingLoading.value = true
  try {
    embeddingProfiles.value = await listEmbeddingProfiles()
  } finally {
    embeddingLoading.value = false
  }
}

function openEmCreate() {
  emDlg.value = {
    visible: true, editingId: '', name: '', api_url: '',
    api_key: '', has_key: false, model: '', models: [],
    fetching: false, testing: false, saving: false, testResult: null,
  }
}

function openEmEdit(p: EmbeddingProfile) {
  emDlg.value = {
    visible: true, editingId: p.id, name: p.name, api_url: p.api_url,
    api_key: '', has_key: p.has_key, model: p.model, models: [],
    fetching: false, testing: false, saving: false, testResult: null,
  }
}

/** 拉取模型列表：Embedding 的服务地址就是 OpenAI 兼容 base（含 /v1） */
async function fetchEmModels() {
  const d = emDlg.value
  if (!d.api_url.trim()) return ElMessage.warning('请先填写服务地址')
  d.fetching = true
  try {
    const res = await fetchExternalModels(d.api_url.trim(), d.api_key.trim(), { kind: 'embedding', profile_id: d.editingId })
    if (!res.ok) {
      ElMessage.error(res.message || '拉取失败')
      return
    }
    d.models = res.models
    if (!res.models.length) ElMessage.warning('服务未返回任何模型')
    else ElMessage.success(`已拉取 ${res.models.length} 个模型`)
  } catch (e: any) {
    ElMessage.error(e?.message || '拉取失败')
  } finally {
    d.fetching = false
  }
}

async function runEmTest() {
  const d = emDlg.value
  if (!d.api_url.trim() || !d.model.trim()) return ElMessage.warning('请先填写服务地址与模型名')
  d.testResult = null
  d.testing = true
  try {
    d.testResult = await testEmbedding({
      base_url: d.api_url,
      api_key: d.api_key,
      model: d.model,
      profile_id: d.editingId,
    })
  } catch (e: any) {
    d.testResult = { ok: false, message: e?.message || '请求失败' }
  } finally {
    d.testing = false
  }
}

async function saveEmProfile() {
  const d = emDlg.value
  if (!d.name.trim()) return ElMessage.warning('请填写配置名称')
  if (!d.api_url.trim()) return ElMessage.warning('请填写服务地址')
  if (!d.model.trim()) return ElMessage.warning('请填写模型名')
  d.saving = true
  try {
    const payload = { name: d.name.trim(), api_url: d.api_url.trim(), api_key: d.api_key, model: d.model.trim() }
    if (d.editingId) {
      await updateEmbeddingProfile(d.editingId, payload)
      ElMessage.success('Embedding 配置已更新')
    } else {
      await createEmbeddingProfile(payload)
      ElMessage.success('配置已创建，点击「生效」切换当前使用的模型')
    }
    d.visible = false
    await loadEmbeddingProfiles()
  } finally {
    d.saving = false
  }
}

async function removeEmProfile(p: EmbeddingProfile) {
  await ElMessageBox.confirm(`确定删除 Embedding 配置「${p.name}」？`, '删除确认', { type: 'warning' })
  await deleteEmbeddingProfile(p.id)
  ElMessage.success('已删除')
  await loadEmbeddingProfiles()
}

async function enableEmProfile(p: EmbeddingProfile) {
  await enableEmbeddingProfile(p.id)
  ElMessage.success(`已切换生效：${p.name}`)
  await loadEmbeddingProfiles()
}

/** Rerank 拉取模型列表：api_url 为完整 /rerank 路径，去掉尾部得到模型列表 base */
async function fetchRrModels() {
  const d = rrDlg.value
  if (!d.api_url.trim()) return ElMessage.warning('请先填写服务地址')
  const base = d.api_url.trim().replace(/\/rerank\/?$/i, '')
  if (base === d.api_url.trim() && !/\/v\d+$/i.test(base)) {
    ElMessage.warning('服务地址需为完整 /rerank 路径或以 /v1 结尾的 base')
    return
  }
  d.fetching = true
  try {
    const res = await fetchExternalModels(base, d.api_key.trim(), { kind: 'rerank', profile_id: d.editingId })
    if (!res.ok) {
      ElMessage.error(res.message || '拉取失败')
      return
    }
    d.models = res.models
    if (!res.models.length) ElMessage.warning('服务未返回任何模型')
    else ElMessage.success(`已拉取 ${res.models.length} 个模型`)
  } catch (e: any) {
    ElMessage.error(e?.message || '拉取失败')
  } finally {
    d.fetching = false
  }
}

onMounted(async () => {
  // 互不依赖的请求并发拉取：原来逐个 await，首屏耗时等于多次往返相加
  const [settings] = await Promise.all([
    getSettings(),
    loadProfiles(),
    loadLlmProfiles(),
    loadMenuConfig(),
    loadBotSection(),
    loadRerankProfiles(),
    loadRagflowProfiles(),
    loadEmbeddingProfiles(),
  ])
  form.value = settings
})

async function save(k: string) {
  const item = form.value[k]
  if (!item) return
  if (item.is_secret && item.is_set && (!item.value || item.value.includes('****'))) {
    ElMessage.info('未填写新值，保留已配置的密钥')
    return
  }
  saving.value = k
  try {
    await setSetting({ key: k, value: item.value })
    form.value = await getSettings()
    if (k === 'site_name' || k === 'site_logo') window.dispatchEvent(new CustomEvent('site-branding-changed'))
    ElMessage.success('已保存')
  } finally {
    saving.value = null
  }
}

// ============ 站点图标：本地上传 → canvas 裁剪压缩为 128×128 data URL ============
const siteLogoInput = ref<HTMLInputElement | null>(null)

function onSiteLogoFile(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (!file.type.startsWith('image/')) {
    ElMessage.warning('请选择图片文件（PNG / JPG / WebP 等）')
    return
  }
  if (file.size > 8 * 1024 * 1024) {
    ElMessage.warning('图片过大（超过 8MB），请更换较小的图片')
    return
  }
  const reader = new FileReader()
  reader.onload = () => {
    const img = new Image()
    img.onload = () => {
      const size = Math.min(img.width, img.height)
      const canvas = document.createElement('canvas')
      canvas.width = 128
      canvas.height = 128
      const ctx = canvas.getContext('2d')
      if (!ctx) {
        ElMessage.error('图片处理失败')
        return
      }
      ctx.drawImage(img, (img.width - size) / 2, (img.height - size) / 2, size, size, 0, 0, 128, 128)
      form.value.site_logo.value = canvas.toDataURL('image/png')
      ElMessage.success('图标已生成，点击「保存图标」生效')
    }
    img.onerror = () => ElMessage.error('图片读取失败')
    img.src = String(reader.result)
  }
  reader.onerror = () => ElMessage.error('图片读取失败')
  reader.readAsDataURL(file)
}

function clearSiteLogo() {
  form.value.site_logo.value = ''
}

// ============ Dify 多配置（多条只能生效一条） ============
const profiles = ref<DifyProfile[]>([])
const profileDialogVisible = ref(false)
const savingProfile = ref(false)
const editingProfile = ref<{ id: string | null; name: string; base_url: string; api_key: string; dataset_ids: string }>({
  id: null, name: '', base_url: '', api_key: '', dataset_ids: '',
})

async function loadProfiles() {
  try {
    profiles.value = await listDifyProfiles()
  } catch {
    profiles.value = []
  }
}

function openProfileDialog(row?: Partial<DifyProfile> & { id?: string }) {
  if (row) {
    editingProfile.value = {
      id: row.id ?? null, name: row.name ?? '', base_url: row.base_url ?? '',
      api_key: '', dataset_ids: row.dataset_ids ?? '',
    }
  } else {
    editingProfile.value = { id: null, name: '', base_url: '', api_key: '', dataset_ids: '' }
  }
  profileDialogVisible.value = true
}

async function saveProfile() {
  if (!editingProfile.value.name.trim()) {
    ElMessage.warning('请填写配置名称')
    return
  }
  savingProfile.value = true
  try {
    const data = {
      name: editingProfile.value.name,
      base_url: editingProfile.value.base_url,
      api_key: editingProfile.value.api_key,
      dataset_ids: editingProfile.value.dataset_ids,
    }
    if (editingProfile.value.id) {
      await updateDifyProfile(editingProfile.value.id, data)
      ElMessage.success('配置已更新')
    } else {
      await createDifyProfile(data)
      ElMessage.success('配置已创建')
    }
    profileDialogVisible.value = false
    await loadProfiles()
  } finally {
    savingProfile.value = false
  }
}

async function removeProfile(id: string) {
  await ElMessageBox.confirm('确认删除该配置？', '提示', { type: 'warning' })
  await deleteDifyProfile(id)
  ElMessage.success('已删除')
  await loadProfiles()
}

// 配置弹窗内连通性测试（编辑态 Key 为掩码时由后端回退已存 Key）
const profileTest = ref({ testing: false, result: null as TestDifyResult | null })

async function runProfileTest() {
  profileTest.value.result = null
  profileTest.value.testing = true
  try {
    profileTest.value.result = await testDify({
      base_url: editingProfile.value.base_url,
      api_key: editingProfile.value.api_key,
      profile_id: editingProfile.value.id || '',
    })
  } catch (e: any) {
    profileTest.value.result = { ok: false, message: e?.message || '请求失败' }
  } finally {
    profileTest.value.testing = false
  }
}

async function enableProfile(id: string) {
  await enableDifyProfile(id)
  ElMessage.success('已切换生效配置')
  await loadProfiles()
}

// ============ 钉钉配置（弹窗配置 + 连通性测试） ============
const dingtalkConfigured = computed(() =>
  !!(form.value.dingtalk_app_key?.is_set && form.value.dingtalk_app_secret?.is_set && form.value.dingtalk_operator_union_id?.is_set))

const dtModal = ref({
  visible: false,
  app_key: '', app_secret: '', operator_union_id: '',
  testing: false, saving: false,
  testResult: null as TestDingtalkResult | null,
})

function openDingtalkModal() {
  dtModal.value = {
    visible: true,
    // Secret 不回显；其余回显已存值
    app_key: form.value.dingtalk_app_key?.value || '',
    app_secret: '',
    operator_union_id: form.value.dingtalk_operator_union_id?.value || '',
    testing: false, saving: false, testResult: null,
  }
}

async function runDingtalkTest() {
  dtModal.value.testResult = null
  dtModal.value.testing = true
  try {
    dtModal.value.testResult = await testDingtalk({
      app_key: dtModal.value.app_key,
      app_secret: dtModal.value.app_secret,
      operator_union_id: dtModal.value.operator_union_id,
    })
  } catch (e: any) {
    dtModal.value.testResult = { ok: false, message: e?.message || '请求失败' }
  } finally {
    dtModal.value.testing = false
  }
}

async function saveDingtalkModal() {
  // Dify 风格：保存前先验证连通性，失败则不落库
  await runDingtalkTest()
  if (!dtModal.value.testResult?.ok) return
  dtModal.value.saving = true
  try {
    const m = dtModal.value
    const items: Array<[string, string]> = [
      ['dingtalk_app_key', m.app_key.trim()],
      ['dingtalk_operator_union_id', m.operator_union_id.trim()],
    ]
    // Secret 已配置但弹窗留空 → 跳过，避免空值清掉已存 Secret
    if (!(form.value.dingtalk_app_secret?.is_set && !m.app_secret)) items.push(['dingtalk_app_secret', m.app_secret.trim()])
    for (const [k, v] of items) await setSetting({ key: k, value: v })
    form.value = await getSettings()
    ElMessage.success('钉钉配置已保存')
    dtModal.value.visible = false
  } finally {
    dtModal.value.saving = false
  }
}

// ============ 钉钉扫码登录开关（setting: dingtalk_qr_login_enabled，未设置时缺省开启） ============
const qrLoginEnabled = computed(() =>
  (form.value.dingtalk_qr_login_enabled?.value ?? 'true').trim().toLowerCase() !== 'false')
const qrLoginSaving = ref(false)

async function saveQrLogin(val: string | number | boolean) {
  qrLoginSaving.value = true
  try {
    await setSetting({ key: 'dingtalk_qr_login_enabled', value: val ? 'true' : 'false' })
    form.value = await getSettings()
    ElMessage.success(val ? '已开启钉钉扫码登录' : '已关闭钉钉扫码登录')
  } finally {
    qrLoginSaving.value = false
  }
}

// ============ 钉钉机器人 & H5 免登（corpId / 开关 / 白名单 / 运行状态） ============
const botForm = ref({ corp_id: '', enabled: false, allow_users: '' })
const botStatus = ref<DingtalkBotStatus | null>(null)
const qaPublicUrl = `${window.location.origin}/qa`

async function loadBotSection() {
  botForm.value = {
    corp_id: form.value.dingtalk_corp_id?.value || '',
    enabled: (form.value.dingtalk_bot_enabled?.value || '').trim().toLowerCase() === 'true',
    allow_users: form.value.dingtalk_bot_allow_users?.value || '',
  }
  try {
    botStatus.value = await getDingtalkBotStatus()
  } catch {
    botStatus.value = null
  }
}

async function saveBotSection() {
  await setSetting({ key: 'dingtalk_corp_id', value: botForm.value.corp_id.trim() })
  await setSetting({ key: 'dingtalk_bot_enabled', value: botForm.value.enabled ? 'true' : 'false' })
  await setSetting({ key: 'dingtalk_bot_allow_users', value: botForm.value.allow_users.trim() })
  form.value = await getSettings()
  ElMessage.success('已保存，机器人按开关与凭证热启停')
  await loadBotSection()
}

async function copyQaUrl() {
  try {
    await navigator.clipboard.writeText(qaPublicUrl)
    ElMessage.success('独立问答地址已复制')
  } catch {
    ElMessage.error('复制失败，请手动复制：' + qaPublicUrl)
  }
}

const mineruTest = ref({ testing: false, result: null as TestMinerUResult | null })

async function runMineruTest() {
  mineruTest.value.result = null
  mineruTest.value.testing = true
  try {
    mineruTest.value.result = await testMineru({ api_key: (form.value.mineru_api_key?.value || '').includes('****') ? '' : form.value.mineru_api_key?.value })
  } catch (e: any) {
    mineruTest.value.result = { ok: false, message: e?.message || '请求失败' }
  } finally {
    mineruTest.value.testing = false
  }
}

// ============ 菜单配置（侧边栏功能区菜单显隐） ============
const router = useRouter()

interface MenuConfigItem {
  path: string
  title: string
  visible: boolean
}

// 功能区全部菜单（与路由 meta 保持一致；隐藏菜单显示为子级缩进）
const menuConfigItems = ref<MenuConfigItem[]>([])
const menuLoading = ref(false)
const menuSaving = ref(false)

async function loadMenuConfig() {
  menuLoading.value = true
  try {
    const res = await getMenuVisibility()
    const hidden = res.hidden || []
    // 取路由中全部功能区菜单（含二级子菜单），生成配置列表
    const items = router
      .getRoutes()
      .filter((r) => r.meta?.title && !r.meta?.hidden && r.name && r.meta?.group === 'feature')
      .map((r) => ({
        path: r.path,
        title: r.meta!.title as string,
        visible: !hidden.includes(r.path),
      }))
    // 保持路由定义顺序
    const orderedPaths = ['/chat', '/deap-agent', '/hiagent', '/collection', '/knowledge-sources', '/collection/dingtalk', '/collection/knowledge-center', '/collection/queue', '/process', '/process/engine', '/apply', '/operate', '/operate/dashboard', '/operate/qa', '/operate/corrections', '/govern', '/govern/gaps', '/govern/review']
    items.sort((a, b) => {
      const ia = orderedPaths.indexOf(a.path)
      const ib = orderedPaths.indexOf(b.path)
      return (ia === -1 ? 999 : ia) - (ib === -1 ? 999 : ib)
    })
    menuConfigItems.value = items
  } catch (e: any) {
    ElMessage.error(e?.message || '菜单配置加载失败')
    menuConfigItems.value = []
  } finally {
    menuLoading.value = false
  }
}

// 开关切换：立即保存（整体覆盖式更新）
async function onMenuToggle(item: MenuConfigItem) {
  const hidden = menuConfigItems.value.filter((x) => !x.visible).map((x) => x.path)
  menuSaving.value = true
  try {
    await setMenuVisibility({ hidden })
    ElMessage.success(`「${item.title}」已${item.visible ? '显示' : '隐藏'}，刷新页面后侧边栏生效`)
  } catch (e: any) {
    // 保存失败时回滚开关状态
    item.visible = !item.visible
    ElMessage.error(e?.message || '保存失败')
  } finally {
    menuSaving.value = false
  }
}

const visibleMenuCount = computed(() => menuConfigItems.value.filter((x) => x.visible).length)
</script>

<template>
  <PageContainer title="系统配置">
    <p class="pg-sub">智能体的大脑与全链路 AI 节点的模型管理：问答模型可选可换，判定类节点（分诊/质检/命名校验/打标）绑定低温度模型并开启结构化输出。</p>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="图片组件" name="image-components" lazy><ImageComponentsPanel /></el-tab-pane>
      <!-- ============ 接入配置（真实表单） ============ -->
      <el-tab-pane label="接入配置" name="access">
        <el-form label-width="140px">
          <el-divider content-position="left">站点外观</el-divider>
          <input ref="siteLogoInput" type="file" accept="image/*" style="display: none" @change="onSiteLogoFile" />
          <el-form-item label="站点名称">
            <div class="field-row">
              <el-input v-model="form.site_name.value" placeholder="默认：知识治理专家" maxlength="30" style="max-width: 280px" />
              <el-button type="primary" :loading="saving === 'site_name'" @click="save('site_name')">保存</el-button>
            </div>
            <div class="field-hint">显示在侧边栏 Logo 与浏览器标题；留空恢复默认。</div>
          </el-form-item>
          <el-form-item label="站点图标">
            <div class="site-logo-row">
              <div class="site-logo-preview">
                <img v-if="form.site_logo.value" :src="form.site_logo.value" alt="站点图标预览" />
                <el-icon v-else :size="22" color="#fff"><i class="el-icon-stamp" /></el-icon>
              </div>
              <div class="site-logo-actions">
                <div class="site-logo-btns">
                  <el-button size="small" @click="siteLogoInput?.click()">上传图片</el-button>
                  <el-button v-if="form.site_logo.value" size="small" text type="danger" @click="clearSiteLogo">恢复默认图标</el-button>
                  <el-button size="small" type="primary" :loading="saving === 'site_logo'" @click="save('site_logo')">保存图标</el-button>
                </div>
                <div class="field-hint">侧边栏 Logo 图标；自动裁剪压缩为 128×128，留空使用默认印章图标。</div>
              </div>
            </div>
          </el-form-item>

          <el-divider content-position="left">LLM 大模型</el-divider>
          <el-form-item label="模型管理">
            <div class="profile-box">
              <div class="profile-toolbar">
                <el-button type="primary" size="small" @click="openLlmCreate">+ 新增模型配置</el-button>
              </div>
              <el-table :data="llmProfiles" size="small" v-loading="llmLoading" style="margin-top: 8px" empty-text="暂无模型配置，点击「新增模型配置」">
                <el-table-column label="名称" min-width="150">
                  <template #default="{ row }">
                    <b>{{ row.name }}</b>
                    <el-tag size="small" effect="plain" style="margin-left: 6px">{{ providerLabel(row.provider) }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="base_url" label="API 端点" min-width="170" show-overflow-tooltip />
                <el-table-column label="API Key" width="110">
                  <template #default="{ row }">
                    <span class="mono">{{ row.has_key ? row.api_key : '未配置' }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="生效模型" min-width="220">
                  <template #default="{ row }">
                    <template v-if="row.models?.length">
                      <el-tag
                        v-for="m in row.models.filter((x: LlmModelEntry) => x.enabled)"
                        :key="m.name"
                        size="small"
                        :type="m.is_default ? 'primary' : 'info'"
                        effect="light"
                        style="margin: 2px"
                      >
                        {{ m.name }}<span v-if="m.is_default"> · 默认</span>
                      </el-tag>
                      <span v-if="!row.models.some((x: LlmModelEntry) => x.enabled)" class="field-hint">无生效模型</span>
                    </template>
                    <span v-else class="field-hint">未拉取模型</span>
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="110">
                  <template #default="{ row }">
                    <el-button type="primary" link size="small" @click="openLlmEdit(row as LlmProfile)">编辑</el-button>
                    <el-button type="danger" link size="small" @click="removeLlmProfile(row as LlmProfile)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
              <div class="field-hint" style="margin-top: 6px">
                勾选「生效」的模型会出现在智能问答的模型选项中；「默认」模型为新会话默认使用，全局唯一。
              </div>
            </div>
          </el-form-item>

          <el-divider content-position="left">Dify 知识库</el-divider>
          <el-form-item label="文件上传上限">
            <div>
              <div style="display:flex;align-items:center;gap:10px">
                <el-input v-model="form.dify_upload_max_mb.value" type="number" min="1" max="1024" aria-label="Dify 单文件上传上限 MB" style="width:140px" />
                <span>MB</span>
                <el-button type="primary" :loading="saving === 'dify_upload_max_mb'" @click="save('dify_upload_max_mb')">保存</el-button>
              </div>
              <div class="field-hint">与 Dify 的 UPLOAD_FILE_SIZE_LIMIT 和网关限制保持一致；此处不会修改 Dify 服务配置。超限文件直接提示，不转为纯文本。</div>
            </div>
          </el-form-item>
          <el-form-item label="配置列表">
            <div class="profile-box">
              <div class="profile-toolbar">
                <el-button size="small" type="primary" @click="openProfileDialog()">+ 新增配置</el-button>
              </div>
              <el-table v-if="profiles.length" :data="profiles" size="small" style="margin-top: 8px">
                <el-table-column label="生效" width="70">
                  <template #default="{ row }">
                    <el-radio :model-value="!!row.enabled" :value="true" :aria-label="`启用 ${row.name}`" @change="enableProfile(row.id)">&nbsp;</el-radio>
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名称" min-width="120" />
                <el-table-column prop="base_url" label="服务地址" min-width="200" />
                <el-table-column label="API Key" min-width="140">
                  <template #default="{ row }">
                    <span v-if="row.api_key" class="mono">{{ row.api_key }}</span>
                    <span v-else class="muted">未设置</span>
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="120">
                  <template #default="{ row }">
                    <el-button size="small" link type="primary" @click="openProfileDialog(row)">编辑</el-button>
                    <el-button size="small" link type="danger" @click="removeProfile(row.id)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
              <div v-else class="empty">尚无配置，请点击「新增配置」添加 Dify 实例。</div>
              <div class="field-hint" style="margin-top: 6px">此处只维护 Dify「连接」（服务地址 + API Key）；启用某条即同步为系统生效连接，只能生效一条。具体检索/同步哪些知识库，请到「知识源管理」登记后按需选择。</div>
            </div>
          </el-form-item>

          <el-divider content-position="left">RAGFlow 知识库</el-divider>
          <el-form-item label="配置列表">
            <div class="profile-box">
              <div class="profile-toolbar">
                <el-button size="small" type="primary" @click="openRfCreate">+ 新增配置</el-button>
              </div>
              <el-table v-if="ragflowProfiles.length" :data="ragflowProfiles" size="small" v-loading="ragflowLoading" style="margin-top: 8px">
                <el-table-column label="生效" width="70">
                  <template #default="{ row }">
                    <el-radio :model-value="!!row.enabled" :value="true" :aria-label="`启用 ${row.name}`" @change="enableRfProfile(row as RagflowProfile)">&nbsp;</el-radio>
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名称" min-width="140">
                  <template #default="{ row }">
                    <b>{{ row.name }}</b>
                    <el-tag v-if="row.enabled" size="small" type="success" effect="light" style="margin-left: 6px">生效中</el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="base_url" label="服务地址" min-width="220" show-overflow-tooltip />
                <el-table-column label="API Key" min-width="110">
                  <template #default="{ row }">
                    <span v-if="row.has_key" class="mono">{{ row.api_key }}</span>
                    <span v-else class="muted">未设置</span>
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="110">
                  <template #default="{ row }">
                    <el-button size="small" link type="primary" @click="openRfEdit(row as RagflowProfile)">编辑</el-button>
                    <el-button size="small" link type="danger" @click="removeRfProfile(row as RagflowProfile)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
              <div v-else class="empty">尚无 RAGFlow 配置，点击「新增 RAGFlow 配置」添加环境连接。</div>
              <div class="field-hint" style="margin-top: 6px">RAGFlow 与 Dify 并列作为外部知识库引擎，可配置多条连接（如测试/生产环境），单选切换生效；服务地址需含端口与 /api/v1 后缀。具体哪些库可用请到「知识源管理」以 RAGFlow 类型登记。</div>
            </div>
          </el-form-item>

          <el-divider content-position="left">Embedding 向量模型</el-divider>
          <el-form-item label="配置列表">
            <div class="profile-box">
              <div class="profile-toolbar">
                <el-button size="small" type="primary" @click="openEmCreate">+ 新增配置</el-button>
              </div>
              <el-table v-if="embeddingProfiles.length" :data="embeddingProfiles" size="small" v-loading="embeddingLoading" style="margin-top: 8px">
                <el-table-column label="生效" width="70">
                  <template #default="{ row }">
                    <el-radio :model-value="!!row.enabled" :value="true" :aria-label="`启用 ${row.name}`" @change="enableEmProfile(row as EmbeddingProfile)">&nbsp;</el-radio>
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名称" min-width="140">
                  <template #default="{ row }">
                    <b>{{ row.name }}</b>
                    <el-tag v-if="row.enabled" size="small" type="success" effect="light" style="margin-left: 6px">生效中</el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="api_url" label="服务地址" min-width="200" show-overflow-tooltip />
                <el-table-column prop="model" label="模型名" min-width="180" show-overflow-tooltip />
                <el-table-column label="API Key" min-width="110">
                  <template #default="{ row }">
                    <span v-if="row.has_key" class="mono">{{ row.api_key }}</span>
                    <span v-else class="muted">免鉴权</span>
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="110">
                  <template #default="{ row }">
                    <el-button size="small" link type="primary" @click="openEmEdit(row as EmbeddingProfile)">编辑</el-button>
                    <el-button size="small" link type="danger" @click="removeEmProfile(row as EmbeddingProfile)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
              <div v-else class="empty">尚无 Embedding 配置，点击「新增配置」添加；未配置时本地 RAG 检索/入库不可用（Dify/RAGFlow 检索不受影响）。</div>
              <div class="field-hint" style="margin-top: 6px">OpenAI 兼容 /embeddings 端点，可配置多条、单选切换生效；向量维度须与既有 ES 索引一致（当前 1024 维），切换模型需重建索引。</div>
            </div>
          </el-form-item>

          <el-divider content-position="left">Rerank 重排模型（检索结果重排序）</el-divider>
          <el-form-item label="配置列表">
            <div class="profile-box">
              <div class="profile-toolbar">
                <el-button size="small" type="primary" @click="openRrCreate">+ 新增重排配置</el-button>
              </div>
              <el-table v-if="rerankProfiles.length" :data="rerankProfiles" size="small" v-loading="rerankLoading" style="margin-top: 8px">
                <el-table-column label="默认模型" width="84">
                  <template #default="{ row }">
                    <el-switch
                      :model-value="!!row.enabled"
                      :aria-label="`设为默认模型 ${row.name}`"
                      @change="(v: string | number | boolean) => enableRrProfile(row as RerankProfile, !!v)"
                    />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名称" min-width="140">
                  <template #default="{ row }">
                    <b>{{ row.name }}</b>
                    <el-tag v-if="row.enabled" size="small" type="success" effect="light" style="margin-left: 6px">生效中</el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="api_url" label="服务地址" min-width="200" show-overflow-tooltip />
                <el-table-column prop="model" label="模型名" min-width="170" show-overflow-tooltip />
                <el-table-column label="API Key" min-width="110">
                  <template #default="{ row }">
                    <span v-if="row.has_key" class="mono">{{ row.api_key }}</span>
                    <span v-else class="muted">免鉴权</span>
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="110">
                  <template #default="{ row }">
                    <el-button size="small" link type="primary" @click="openRrEdit(row as RerankProfile)">编辑</el-button>
                    <el-button size="small" link type="danger" @click="removeRrProfile(row as RerankProfile)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
              <div v-else class="empty">尚无重排配置，点击「新增重排配置」添加；未配置时检索跳过重排（RRF/BM25 排序兜底）。</div>
              <div class="field-hint" style="margin-top: 6px">Jina/SiliconFlow 风格 /rerank 端点，可配置多条，通过开关单选一条作为生效（默认）模型；知识库「检索设置」中只能选择已生效的模型。API Key 非必填（内网/自建服务常免鉴权）。</div>
            </div>
          </el-form-item>

          <el-divider content-position="left">文档解析</el-divider>
          <el-form-item label="MinerU API Key">
            <div class="field-row">
              <el-input v-model="form.mineru_api_key.value" show-password placeholder="未设置" />
              <el-button :loading="mineruTest.testing" @click="runMineruTest">测试连通性</el-button>
              <el-button type="primary" :loading="saving === 'mineru_api_key'" @click="save('mineru_api_key')">保存</el-button>
            </div>
            <el-alert
              v-if="mineruTest.result?.ok"
              type="success"
              :closable="false"
              show-icon
              style="margin-top: 8px"
              :title="`连接成功 · ${mineruTest.result.latency_ms}ms · ${mineruTest.result.message}`"
            />
            <el-alert
              v-else-if="mineruTest.result && !mineruTest.result.ok"
              type="error"
              :closable="false"
              show-icon
              style="margin-top: 8px"
              :title="mineruTest.result.message || '连接失败'"
            />
          </el-form-item>

          <el-divider content-position="left">钉钉（企业知识库数据源）</el-divider>
          <!-- 配置结果展示：具体凭证在弹窗中配置，Secret 脱敏 -->
          <el-form-item label="配置状态">
            <div class="dt-result" :class="{ ok: dingtalkConfigured }">
              <div class="dt-row">
                <span class="dt-label">状态</span>
                <el-tag :type="dingtalkConfigured ? 'success' : 'info'" size="small" effect="light">
                  {{ dingtalkConfigured ? '已配置' : '未配置' }}
                </el-tag>
              </div>
              <div class="dt-row">
                <span class="dt-label">AppKey</span>
                <span class="mono">{{ form.dingtalk_app_key?.is_set ? form.dingtalk_app_key.value : '未配置' }}</span>
              </div>
              <div class="dt-row">
                <span class="dt-label">AppSecret</span>
                <span class="mono">{{ form.dingtalk_app_secret?.is_set ? '••••••••（已配置，不显示）' : '未配置' }}</span>
              </div>
              <div class="dt-row">
                <span class="dt-label">服务账号 union_id（兜底）</span>
                <span class="mono">{{ form.dingtalk_operator_union_id?.is_set ? form.dingtalk_operator_union_id.value : '未配置' }}</span>
              </div>
              <div class="dt-row">
                <span class="dt-label">扫码登录</span>
                <el-switch
                  :model-value="qrLoginEnabled"
                  :loading="qrLoginSaving"
                  aria-label="钉钉扫码登录开关"
                  @change="saveQrLogin"
                />
                <span class="field-hint">开启后登录页展示「钉钉扫码」页签，用户可扫码登录 / 绑定钉钉身份</span>
              </div>
              <div class="dt-row dt-foot">
                <span class="field-hint">运营看板的"企业知识"指标来源于此用户可见的钉钉知识库；需授予应用「知识库读权限」。</span>
                <el-button type="primary" size="small" @click="openDingtalkModal">{{ dingtalkConfigured ? '修改配置' : '配置' }}</el-button>
              </div>
            </div>
          </el-form-item>

          <el-divider content-position="left">钉钉机器人 & H5 免登</el-divider>
          <el-form-item label="机器人问答与独立页接入">
            <div class="dt-result" :class="{ ok: !!botStatus?.connected }">
              <div class="dt-row">
                <span class="dt-label">corpId</span>
                <el-input v-model="botForm.corp_id" size="small" style="width: 260px"
                          placeholder="企业 corpId（H5 免登必填）" />
              </div>
              <div class="dt-row">
                <span class="dt-label">机器人开关</span>
                <el-switch v-model="botForm.enabled" active-text="开启" inactive-text="关闭" />
              </div>
              <div class="dt-row">
                <span class="dt-label">白名单</span>
                <el-input v-model="botForm.allow_users" size="small" style="width: 260px"
                          placeholder="userid 逗号分隔，空=全员可用" />
              </div>
              <div class="dt-row">
                <span class="dt-label">运行状态</span>
                <el-tag :type="botStatus?.connected ? 'success' : botStatus?.enabled ? 'warning' : 'info'"
                        size="small" effect="light">
                  {{ botStatus?.connected ? '已连接' : botStatus?.enabled ? '启用中·连接建立中' : '未启用' }}
                </el-tag>
                <span v-if="botStatus?.started_at" class="mono">启动于 {{ botStatus.started_at }}</span>
                <el-button link type="primary" size="small" @click="loadBotSection">刷新</el-button>
              </div>
              <div v-if="botStatus?.last_error" class="dt-row">
                <span class="field-hint">{{ botStatus.last_error }}</span>
              </div>
              <div class="dt-row">
                <span class="dt-label">H5 地址</span>
                <span class="mono">{{ qaPublicUrl }}</span>
                <el-button link type="primary" size="small" @click="copyQaUrl">复制</el-button>
              </div>
              <div class="dt-row dt-foot">
                <span class="field-hint">机器人走 Stream 长连接（内网无需公网入口）；将 H5 地址配置为钉钉微应用首页即可免登访问独立问答页。</span>
                <el-button type="primary" size="small" @click="saveBotSection">保存</el-button>
              </div>
            </div>
          </el-form-item>
        </el-form>
      </el-tab-pane>
      <!-- ============ 菜单配置（侧边栏功能区菜单显隐） ============ -->
      <el-tab-pane label="菜单配置" name="menu">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>功能区菜单显示配置 <el-tag size="small" type="primary">显示 {{ visibleMenuCount }}/{{ menuConfigItems.length }}</el-tag></span>
              <el-button size="small" :loading="menuLoading" @click="loadMenuConfig">🔄 刷新</el-button>
            </div>
          </template>
          <el-table :data="menuConfigItems" v-loading="menuLoading" style="width: 100%">
            <el-table-column prop="title" label="菜单项" min-width="160" />
            <el-table-column prop="path" label="路径" min-width="160">
              <template #default="{ row }"><span class="mono">{{ row.path }}</span></template>
            </el-table-column>
            <el-table-column label="显示 / 隐藏" width="120">
              <template #default="{ row }">
                <el-switch
                  v-model="row.visible"
                  :loading="menuSaving"
                  :disabled="row.path === '/chat'"
                  inline-prompt
                  active-text="显示"
                  inactive-text="隐藏"
                  @change="onMenuToggle(row as MenuConfigItem)"
                />
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.path === '/chat'" size="small" type="info">固定</el-tag>
                <el-tag v-else-if="row.visible" size="small" type="success">显示</el-tag>
                <el-tag v-else size="small" type="danger">隐藏</el-tag>
              </template>
            </el-table-column>
          </el-table>
          <div class="field-hint" style="margin-top: 8px">
            关闭开关后，对应菜单将从侧边栏「功能区」隐藏（页面路由仍可访问）；「智能问答」为默认首页，固定显示。配置保存后刷新页面生效。
          </div>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- LLM 模型配置弹窗：供应商 → Key → 拉取模型 → 复选框勾选生效/默认 -->
    <el-dialog v-model="llmDlg.visible" :title="llmDlg.editingId ? '编辑模型配置' : '新增模型配置'" width="640px" destroy-on-close>
      <el-form label-position="top" @submit.prevent>
        <el-form-item required label="模型供应商">
          <el-select v-model="llmDlg.provider" style="width: 100%" :disabled="!!llmDlg.editingId" @change="onProviderChange">
            <el-option v-for="p in llmProviderPresets" :key="p.key" :label="p.name" :value="p.key" />
          </el-select>
        </el-form-item>
        <el-form-item required label="配置名称">
          <el-input v-model="llmDlg.name" placeholder="如：DeepSeek 生产环境" />
        </el-form-item>
        <el-form-item required label="API Base URL">
          <el-input v-model="llmDlg.base_url" placeholder="https://api.deepseek.com/v1" />
        </el-form-item>
        <el-form-item :required="!llmDlg.editingId" label="API Key">
          <el-input v-model="llmDlg.api_key" show-password :placeholder="llmDlg.has_key ? '••••••••' : '在此输入您的 API Key'" />
          <div v-if="llmDlg.editingId" class="field-hint">留空表示不修改已保存的 Key</div>
        </el-form-item>
        <div class="dlg-actions">
          <el-button size="small" type="primary" plain :loading="llmDlg.fetching" @click="fetchLlmModels">拉取模型列表</el-button>
          <a v-if="llmDocLink" :href="llmDocLink" target="_blank" class="doc-link">获取 API Key ↗</a>
        </div>
        <el-alert
          v-if="llmDlg.testResult?.ok"
          type="success" :closable="false" show-icon style="margin-top: 8px"
          :title="`连接成功 · ${llmDlg.testResult.latency_ms}ms · 模型回复：${(llmDlg.testResult.reply || '').slice(0, 50)}`"
        />
        <el-alert
          v-else-if="llmDlg.testResult && !llmDlg.testResult.ok"
          type="error" :closable="false" show-icon style="margin-top: 8px"
          :title="llmDlg.testResult.message || '连接失败'"
        />
        <el-form-item v-if="llmDlg.models.length" label="可用模型（复选框=生效出现在问答选项；开关=设为新会话默认）" style="margin-top: 12px">
          <div class="model-pick-list">
            <div v-for="m in llmDlg.models" :key="m.name" class="mpl-row">
              <el-checkbox
                v-model="m.enabled"
                @change="(v: any) => { if (!v) m.is_default = false }"
              >{{ m.name }}</el-checkbox>
              <el-switch
                v-model="m.is_default"
                :disabled="!m.enabled"
                active-text="默认"
                inline-prompt
                @change="() => onDefaultToggle(m)"
              />
            </div>
          </div>
        </el-form-item>
        <div v-else class="field-hint" style="margin-top: 8px">填写 API Base URL 与 Key 后，点击「拉取模型列表」加载该供应商的推理模型。</div>
      </el-form>
      <template #footer>
        <el-button :loading="llmDlg.testing" @click="testLlmDlg">测试连通性</el-button>
        <el-button @click="llmDlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="llmDlg.saving" @click="saveLlmProfile">保存</el-button>
      </template>
    </el-dialog>

    <!-- Dify 配置编辑弹窗 -->
    <el-dialog v-model="profileDialogVisible" :title="editingProfile.id ? '编辑配置' : '新增配置'" width="520px">
      <el-form label-width="120px" @submit.prevent>
        <el-form-item label="配置名称">
          <el-input v-model="editingProfile.name" placeholder="如：生产环境 / 测试环境" />
        </el-form-item>
        <el-form-item label="API 端点">
          <el-input v-model="editingProfile.base_url" placeholder="http://127.0.0.1:8088/v1" />
          <div class="field-hint">Dify 知识库 Service API 端点，需含端口与 /v1，例如 http://127.0.0.1:8088/v1 或 https://dify.example.com/v1</div>
        </el-form-item>
        <el-form-item label="API Key">
          <el-input v-model="editingProfile.api_key" show-password :placeholder="editingProfile.id ? '••••••••' : 'Dataset API Key，如 dataset-R6qPqf7CfPC0Dbn6uT3LxIKw'" />
        </el-form-item>
        <el-alert
          v-if="profileTest.result?.ok"
          type="success"
          :closable="false"
          show-icon
          :title="`连接成功 · ${profileTest.result.latency_ms}ms · ${profileTest.result.message}`"
        />
        <el-alert
          v-else-if="profileTest.result && !profileTest.result.ok"
          type="error"
          :closable="false"
          show-icon
          :title="profileTest.result.message || '连接失败'"
        />
      </el-form>
      <template #footer>
        <el-button :loading="profileTest.testing" @click="runProfileTest">测试连通性</el-button>
        <el-button @click="profileDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingProfile" @click="saveProfile">保存</el-button>
      </template>
    </el-dialog>

    <!-- Rerank 重排配置编辑弹窗 -->
    <el-dialog v-model="rrDlg.visible" :title="rrDlg.editingId ? '编辑重排配置' : '新增重排配置'" width="560px" destroy-on-close>
      <el-form label-position="top" @submit.prevent>
        <el-form-item required label="配置名称">
          <el-input v-model="rrDlg.name" placeholder="如：BGE-reranker 生产环境" />
        </el-form-item>
        <el-form-item required label="服务地址">
          <el-input v-model="rrDlg.api_url" placeholder="http://10.10.166.81:7775/v1/rerank（需含完整 /rerank 路径）" />
        </el-form-item>
        <el-form-item required label="模型名">
          <div class="model-pick-row">
            <el-select
              v-model="rrDlg.model" filterable allow-create default-first-option
              placeholder="拉取后选择，或直接输入模型名"
              style="flex: 1"
            >
              <el-option v-for="m in rrDlg.models" :key="m" :label="m" :value="m" />
            </el-select>
            <el-button :loading="rrDlg.fetching" @click="fetchRrModels">拉取模型列表</el-button>
          </div>
        </el-form-item>
        <el-form-item label="API Key（非必填）">
          <el-input v-model="rrDlg.api_key" show-password :placeholder="rrDlg.has_key ? '••••••••（留空保持不变）' : '内网/自建服务常免鉴权，可留空'" />
          <div v-if="rrDlg.editingId && rrDlg.has_key" class="field-hint">留空表示不修改已保存的 Key</div>
        </el-form-item>
        <el-alert
          v-if="rrDlg.testResult?.ok"
          type="success" :closable="false" show-icon style="margin-top: 8px"
          :title="`连接成功 · ${rrDlg.testResult.latency_ms}ms · ${rrDlg.testResult.message}`"
        />
        <el-alert
          v-else-if="rrDlg.testResult && !rrDlg.testResult.ok"
          type="error" :closable="false" show-icon style="margin-top: 8px"
          :title="rrDlg.testResult.message || '连接失败'"
        />
      </el-form>
      <template #footer>
        <el-button :loading="rrDlg.testing" @click="runRrTest">测试连通性</el-button>
        <el-button @click="rrDlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="rrDlg.saving" @click="saveRrProfile">保存</el-button>
      </template>
    </el-dialog>

    <!-- Embedding 向量模型配置编辑弹窗 -->
    <el-dialog v-model="emDlg.visible" :title="emDlg.editingId ? '编辑 Embedding 配置' : '新增 Embedding 配置'" width="560px" destroy-on-close>
      <el-form label-position="top" @submit.prevent>
        <el-form-item required label="配置名称">
          <el-input v-model="emDlg.name" placeholder="如：Qwen3-Embedding 生产 / bge-m3 测试" />
        </el-form-item>
        <el-form-item required label="服务地址">
          <el-input v-model="emDlg.api_url" placeholder="如 http://10.10.166.81:8987/v1（OpenAI 兼容，需含 /v1）" />
        </el-form-item>
        <el-form-item required label="模型名">
          <div class="model-pick-row">
            <el-select
              v-model="emDlg.model" filterable allow-create default-first-option
              placeholder="拉取后选择，或直接输入模型名"
              style="flex: 1"
            >
              <el-option v-for="m in emDlg.models" :key="m" :label="m" :value="m" />
            </el-select>
            <el-button :loading="emDlg.fetching" @click="fetchEmModels">拉取模型列表</el-button>
          </div>
        </el-form-item>
        <el-form-item label="API Key（非必填）">
          <el-input v-model="emDlg.api_key" show-password :placeholder="emDlg.has_key ? '••••••••（留空保持不变）' : '内网/自建服务常免鉴权，可留空'" />
          <div v-if="emDlg.editingId && emDlg.has_key" class="field-hint">留空表示不修改已保存的 Key</div>
        </el-form-item>
        <el-alert
          v-if="emDlg.testResult?.ok"
          type="success" :closable="false" show-icon style="margin-top: 8px"
          :title="`连接成功 · ${emDlg.testResult.latency_ms}ms · ${emDlg.testResult.message}`"
        />
        <el-alert
          v-else-if="emDlg.testResult && !emDlg.testResult.ok"
          type="error" :closable="false" show-icon style="margin-top: 8px"
          :title="emDlg.testResult.message || '连接失败'"
        />
      </el-form>
      <template #footer>
        <el-button :loading="emDlg.testing" @click="runEmTest">测试连通性</el-button>
        <el-button @click="emDlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="emDlg.saving" @click="saveEmProfile">保存</el-button>
      </template>
    </el-dialog>

    <!-- RAGFlow 配置编辑弹窗 -->
    <el-dialog v-model="rfDlg.visible" :title="rfDlg.editingId ? '编辑 RAGFlow 配置' : '新增 RAGFlow 配置'" width="560px" destroy-on-close>
      <el-form label-position="top" @submit.prevent>
        <el-form-item required label="配置名称">
          <el-input v-model="rfDlg.name" placeholder="如：RAGFlow 测试环境 / 生产环境" />
        </el-form-item>
        <el-form-item required label="服务地址">
          <el-input v-model="rfDlg.base_url" placeholder="http://127.0.0.1:9380/api/v1（需含端口与 /api/v1 后缀）" />
        </el-form-item>
        <el-form-item required label="API Key">
          <el-input v-model="rfDlg.api_key" show-password :placeholder="rfDlg.has_key ? '••••••••（留空保持不变）' : 'RAGFlow API Key（RAGFlow 接口均需认证）'" />
          <div v-if="rfDlg.editingId && rfDlg.has_key" class="field-hint">留空表示不修改已保存的 Key</div>
        </el-form-item>
        <el-alert
          v-if="rfDlg.testResult?.ok"
          type="success" :closable="false" show-icon style="margin-top: 8px"
          :title="rfDlg.testResult.message || '连接成功'"
        />
        <el-alert
          v-else-if="rfDlg.testResult && !rfDlg.testResult.ok"
          type="error" :closable="false" show-icon style="margin-top: 8px"
          :title="rfDlg.testResult.message || '连接失败'"
        />
      </el-form>
      <template #footer>
        <el-button :loading="rfDlg.testing" @click="runRfTest">测试连通性</el-button>
        <el-button @click="rfDlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="rfDlg.saving" @click="saveRfProfile">保存</el-button>
      </template>
    </el-dialog>

    <!-- 钉钉配置弹窗（AppKey / AppSecret / UnionId + 连通性测试） -->
    <el-dialog v-model="dtModal.visible" title="配置钉钉（企业知识库数据源）" width="520px" destroy-on-close>
      <el-form label-position="top" @submit.prevent>
        <el-form-item label="AppKey">
          <el-input v-model="dtModal.app_key" placeholder="钉钉开放平台应用 AppKey" />
        </el-form-item>
        <el-form-item label="AppSecret">
          <el-input
            v-model="dtModal.app_secret"
            show-password
            :placeholder="form.dingtalk_app_secret?.is_set ? '••••••••' : '钉钉开放平台应用 AppSecret'"
          />
        </el-form-item>
        <el-form-item label="服务账号 union_id（兜底）">
          <el-input v-model="dtModal.operator_union_id" placeholder="具备知识库读权限的用户 UnionId" />
          <div class="field-hint">同步源 owner 未绑定钉钉时使用；建议仅冷启动期使用</div>
        </el-form-item>
        <el-alert
          v-if="dtModal.testResult?.ok"
          type="success"
          :closable="false"
          show-icon
        >
          <template #title>
            连接成功 · {{ dtModal.testResult.latency_ms }}ms · {{ dtModal.testResult.message }}
          </template>
        </el-alert>
        <el-alert
          v-else-if="dtModal.testResult && !dtModal.testResult.ok"
          type="error"
          :closable="false"
          show-icon
          :title="dtModal.testResult.message || '连接失败'"
        />
      </el-form>
      <template #footer>
        <el-button :loading="dtModal.testing" @click="runDingtalkTest">测试连通性</el-button>
        <el-button type="primary" :loading="dtModal.saving" @click="saveDingtalkModal">保存</el-button>
      </template>
    </el-dialog>
  </PageContainer>
</template>

<style scoped>
.pg-sub { color: #6b7280; font-size: 13px; margin-bottom: 16px; line-height: 1.8; }
.card-header { display: flex; align-items: center; justify-content: space-between; }
.field-row { display: flex; gap: 8px; width: 100%; }
.field-row .el-input { flex: 1; }
.field-hint { font-size: 12px; color: #909399; margin-top: 4px; }
/* 站点图标配置 */
.site-logo-row { display: flex; align-items: center; gap: 16px; }
.site-logo-preview {
  width: 48px; height: 48px; border-radius: 8px; flex-shrink: 0;
  display: grid; place-items: center; overflow: hidden;
  background: linear-gradient(135deg, var(--app-brand-blue), #6d28d9);
}
.site-logo-preview img { width: 100%; height: 100%; object-fit: cover; }
.site-logo-actions { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }
.site-logo-btns { display: flex; align-items: center; gap: 8px; }
.model-pick-row { display: flex; align-items: center; gap: 8px; width: 100%; }
.dataset-box { width: 100%; }
.dlg-actions { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.model-pick-list { width: 100%; border: 1px solid #e4e7ed; border-radius: 8px; padding: 6px 12px; max-height: 240px; overflow-y: auto; }
.mpl-row { display: flex; align-items: center; justify-content: space-between; padding: 4px 0; border-bottom: 1px dashed #ebeef5; }
.mpl-row:last-child { border-bottom: none; }
.doc-link { color: var(--el-color-primary); font-size: 13px; text-decoration: none; }
.doc-link:hover { text-decoration: underline; }
.dt-result { width: 100%; border: 1px solid #e4e7ed; border-radius: 10px; padding: 12px 16px; background: #fafbfc; }
.dt-result.ok { border-color: #b3e19d; background: #f7fbf5; }
.dt-row { display: flex; align-items: center; gap: 12px; padding: 4px 0; font-size: 13px; }
.dt-label { width: 120px; color: #909399; flex-shrink: 0; }
.dt-foot { justify-content: space-between; margin-top: 6px; padding-top: 8px; border-top: 1px dashed #e4e7ed; }
.dataset-box { width: 100%; }
.dataset-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.empty { font-size: 12.5px; color: #909399; margin-top: 8px; }
.profile-box { width: 100%; }
.profile-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ds-section-title { font-size: 13px; font-weight: 600; color: #303133; margin-top: 12px; }
.err { font-size: 12px; color: #f56c6c; }
.small { font-size: 12.5px; color: #475569; line-height: 1.8; }
.small.up { color: #16a34a; }
.muted { color: #9ca3af; font-size: 12px; }
.mono { font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: #334155; }
</style>
