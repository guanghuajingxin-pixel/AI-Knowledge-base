<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getAgentConfig, updateAgentConfig,
  getPersona, savePersona,
  getAgentTools, listSkills, saveSkillDoc as putSkillDoc, deleteSkillDoc, importSkillDoc,
  type AgentConfig, type AgentTool, type SkillDoc,
} from '@/api/agent'
import { listEnabledLlmModels } from '@/api/settings'
import { listKnowledgeLibraries, type KnowledgeLibrary } from '@/api/knowledge-library'
import KbPickCard from '@/components/kb/KbPickCard.vue'
import { KB_TYPE_META, kbTypeLabel, kbTypeMeta } from '@/components/kb/type-meta'
import { Search } from '@lucide/vue'

const loading = ref(false)
const saving = ref(false)
const activeTab = ref('basic')
const activeCollapse = ref(['model', 'knowledge', 'tools', 'avatar', 'greeting', 'planning', 'memory', 'deep', 'hyper'])

const form = reactive<AgentConfig>({
  agent_name: '杰克百晓生',
  bot_avatar: '',
  models: [],
  default_model: '',
  retrieval_mode: 'smart',
  knowledge_library_ids: [],
  knowledge_library_disabled_ids: [],
  greeting_enabled: true,
  greeting: '',
  suggested_questions: [],
  follow_up_enabled: true,
  tools_enabled: {},
  planning_enabled: true,
  subagent_enabled: false,
  long_memory_enabled: true,
  temperature: 0,
  top_p: 0.9,
  max_tokens: 4096,
  top_k: 8,
  max_retrieval_rounds: 2,
  external_agents: {
    hiagent: { enabled: false, embed_code: '', url: '' },
    dify: { enabled: false, embed_code: '', url: '' },
    deap: { enabled: false, embed_code: '', url: '' },
  },
})

// ============ 智能体类型页签（内置 / HiAgent / Dify / DEAP） ============
type AgentKind = 'builtin' | 'hiagent' | 'dify' | 'deap'
type ExternalKind = 'hiagent' | 'dify' | 'deap'
const agentKind = ref<AgentKind>('builtin')

const EXTERNAL_META: Record<ExternalKind, { title: string; icon: string; cls: string; desc: string; placeholder: string }> = {
  hiagent: {
    title: 'HiAgent 智能体',
    icon: '🤖',
    cls: 'ico-model',
    desc: '粘贴 HiAgent 平台「嵌入网页」处复制的 iframe 代码（或页面链接），将平台发布的智能体接入本系统；更换智能体时只需替换嵌入代码。',
    placeholder: '<iframe\n  src="https://hiagent.example.com/share/xxxx"\n  style="width: 100%; height: 100%; min-height: 700px"\n  frameborder="0"\n  allow="microphone;clipboard-write">\n</iframe>',
  },
  dify: {
    title: 'Dify 智能体',
    icon: '🌐',
    cls: 'ico-kb',
    desc: '粘贴 Dify 平台「嵌入网站」处复制的 iframe 代码（或页面链接），将 Dify 应用接入本系统；更换智能体时只需替换嵌入代码。',
    placeholder: '<iframe\n src="http://127.0.0.1/agent/eDE91u43v5UPpbt0"\n style="width: 100%; height: 100%; min-height: 700px"\n frameborder="0"\n allow="microphone;clipboard-write">\n</iframe>',
  },
  deap: {
    title: 'DEAP 智能问答',
    icon: '💬',
    cls: 'ico-greet',
    desc: '粘贴钉钉 DEAP 智能问答平台发布后的 H5 页面链接，将 DEAP 智能问答接入本系统；更换智能体时只需替换链接。DEAP 页面需要钉钉登录态，未登录时会跳转钉钉授权，建议在钉钉客户端或已登录钉钉的浏览器中使用。',
    placeholder: 'https://deap-agent.dingtalk.com/h5/index.html?publish_h5=1&code=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx',
  },
}

// 页签 → 外部平台 key 映射（builtin 不展示嵌入面板，映射仅为类型兜底）
const EXTERNAL_KINDS: Record<AgentKind, ExternalKind> = {
  builtin: 'hiagent', hiagent: 'hiagent', dify: 'dify', deap: 'deap',
}

const currentExternal = computed(() => form.external_agents[EXTERNAL_KINDS[agentKind.value]])

const externalMeta = computed(() => EXTERNAL_META[EXTERNAL_KINDS[agentKind.value]])

// 从嵌入代码实时解析页面地址：iframe 片段取 src；直接粘贴 URL 亦可
const parsedEmbedUrl = computed(() => {
  const code = (currentExternal.value.embed_code || '').trim()
  if (!code) return ''
  const m = code.match(/src\s*=\s*["']([^"']+)["']/i)
  if (m) return m[1].trim()
  if (/^https?:\/\//i.test(code)) return code
  return ''
})

// 工具目录（名称/描述来自后端 /agent/tools，开关状态保存于 form.tools_enabled）
const toolCatalog = ref<AgentTool[]>([])

// ============ 知识库范围选择（智能问答的资源项：未选择=不检索） ============
// 知识库抽象层全部库（不含物料库）；「添加知识库」弹窗内按类型筛选后勾选
const kbLibraries = ref<KnowledgeLibrary[]>([])
const kbLibrariesError = ref('')
type KbFilterType = 'document' | 'dify' | 'ragflow'
const KB_FILTERS: Array<{ value: KbFilterType; label: string; match: (l: KnowledgeLibrary) => boolean }> = [
  { value: 'document', label: KB_TYPE_META.document.label, match: (l) => l.library_type === 'document' },
  { value: 'dify', label: KB_TYPE_META.dify.label, match: (l) => l.library_type === 'external' && l.platform === 'dify' },
  { value: 'ragflow', label: KB_TYPE_META.ragflow.label, match: (l) => l.library_type === 'external' && l.platform === 'ragflow' },
]
// 弹窗状态：类型筛选（默认文档库）+ 视图（卡片/列表，默认卡片）+ 弹窗内暂存勾选（确认后写回 form）
const kbDialogVisible = ref(false)
const kbTypeFilter = ref<KbFilterType>('document')
const kbView = ref<'card' | 'list'>('card')
const kbDialogSelection = ref<number[]>([])
const kbFilterDef = computed(() => KB_FILTERS.find((t) => t.value === kbTypeFilter.value)!)
const kbKeyword = ref('')
const kbFilteredLibraries = computed(() => {
  const kw = kbKeyword.value.trim().toLowerCase()
  return kbLibraries.value.filter((l) => {
    if (!kbFilterDef.value.match(l)) return false
    if (!kw) return true
    return l.name.toLowerCase().includes(kw) || String(l.dataset_id || '').toLowerCase().includes(kw)
  })
})
// 全选：勾选/取消当前筛选结果中全部可用库（部分选中时为 indeterminate 态，点击补全为全选）
const kbDialogSelectable = computed(() => kbFilteredLibraries.value.filter((l) => l.enabled))
const kbSelectAllChecked = computed(() => {
  const list = kbDialogSelectable.value
  return list.length > 0 && list.every((l) => kbDialogSelection.value.includes(l.id))
})
const kbSelectAllIndeterminate = computed(() => {
  const list = kbDialogSelectable.value
  const n = list.filter((l) => kbDialogSelection.value.includes(l.id)).length
  return n > 0 && n < list.length
})
function toggleKbSelectAll() {
  const list = kbDialogSelectable.value
  const ids = new Set(kbDialogSelection.value)
  if (kbSelectAllChecked.value) list.forEach((l) => ids.delete(l.id))
  else list.forEach((l) => ids.add(l.id))
  kbDialogSelection.value = [...ids]
}
// 已选库详情（跨类型）：折叠项以标签回显类型与名称
const kbSelectedLibs = computed(() =>
  kbLibraries.value.filter((l) => form.knowledge_library_ids?.includes(l.id)))

function removeKbSelection(id: number) {
  if (!form.knowledge_library_ids) return
  form.knowledge_library_ids = form.knowledge_library_ids.filter((i) => i !== id)
  // 移除绑定时同步清理停用标记，避免脏数据
  if (form.knowledge_library_disabled_ids) {
    form.knowledge_library_disabled_ids = form.knowledge_library_disabled_ids.filter((i) => i !== id)
  }
}

/** 智能体级停用标记（仅本地态，点「保存配置」才持久化）：停用的库不参与检索 */
function isKbDisabled(id: number): boolean {
  return !!form.knowledge_library_disabled_ids?.includes(id)
}

function toggleKbEnabled(id: number, enabled: boolean) {
  if (!form.knowledge_library_disabled_ids) form.knowledge_library_disabled_ids = []
  const set = new Set(form.knowledge_library_disabled_ids)
  if (enabled) set.delete(id)
  else set.add(id)
  form.knowledge_library_disabled_ids = [...set]
}

function isKbDialogSelected(id: number): boolean {
  return kbDialogSelection.value.includes(id)
}

/** 卡片视图整卡点选：切换弹窗内暂存勾选（停用库不可选） */
function toggleKbDialogSelection(lib: KnowledgeLibrary) {
  if (!lib.enabled) return
  const idx = kbDialogSelection.value.indexOf(lib.id)
  if (idx >= 0) kbDialogSelection.value.splice(idx, 1)
  else kbDialogSelection.value.push(lib.id)
}

function openKbDialog() {
  // 打开弹窗：类型回到默认文档库、视图回默认卡片，勾选状态以当前配置初始化
  kbTypeFilter.value = 'document'
  kbView.value = 'card'
  kbKeyword.value = ''
  kbDialogSelection.value = [...(form.knowledge_library_ids || [])]
  kbDialogVisible.value = true
}

function confirmKbDialog() {
  form.knowledge_library_ids = [...kbDialogSelection.value]
  // 停用标记与最新选区求交：新加入的库默认启用，已移除的库清理标记
  const ids = new Set(form.knowledge_library_ids)
  form.knowledge_library_disabled_ids = (form.knowledge_library_disabled_ids || []).filter((i) => ids.has(i))
  kbDialogVisible.value = false
}

async function loadKbLibraries() {
  kbLibrariesError.value = ''
  try {
    // 列表接口已排除物料库（素材管理专用，不参与智能问答检索）
    kbLibraries.value = await listKnowledgeLibraries()
  } catch (e: any) {
    kbLibrariesError.value = e?.message || '加载失败'
    kbLibraries.value = []
  }
}

// 仅可选择系统已接入（生效）的模型；is_default 为系统配置里的默认模型
const modelOptions = ref<Array<{ model: string; profile_id: string; profile_name: string; is_default: boolean }>>([])
// 智能体生效默认模型：管理员点选的 default_model，未设置时跟随列表首个
const effectiveDefaultModel = computed(() => form.default_model || form.models[0] || '')
watch(() => form.models, (models) => {
  if (form.default_model && !models.includes(form.default_model)) form.default_model = ''
}, { deep: true })

async function load() {
  loading.value = true
  try {
    const [cfg, toolsRes] = await Promise.all([getAgentConfig(), getAgentTools().catch(() => null)])
    Object.assign(form, cfg)
    form.knowledge_library_ids = cfg.knowledge_library_ids || []
    form.knowledge_library_disabled_ids = cfg.knowledge_library_disabled_ids || []
    // 外部智能体配置兜底（旧数据可能缺平台 key）
    const ext = (form.external_agents || {}) as Record<string, Partial<AgentConfig['external_agents']['hiagent']>>
    form.external_agents = {
      hiagent: { enabled: false, embed_code: '', url: '', ...ext.hiagent },
      dify: { enabled: false, embed_code: '', url: '', ...ext.dify },
      deap: { enabled: false, embed_code: '', url: '', ...ext.deap },
    }
    // 纯链接保存后后端将 URL 从 embed_code 归档到 url 字段；回填保证编辑框可见（DEAP 主场景为纯链接）
    Object.values(form.external_agents).forEach((e) => {
      if (!e.embed_code && e.url) e.embed_code = e.url
    })
    toolCatalog.value = toolsRes?.tools || []
    // 目录补齐 form.tools_enabled 中缺失的 key（默认启用）
    toolCatalog.value.forEach((t) => {
      if (form.tools_enabled[t.key] === undefined) form.tools_enabled[t.key] = t.enabled
    })
  } catch {
    ElMessage.error('加载智能体配置失败')
  } finally {
    loading.value = false
  }
  // 拉取系统已接入（生效）的模型作为候选，仅可选择、不允许手输模型名
  try {
    const res = await listEnabledLlmModels()
    const seen = new Map<string, { model: string; profile_id: string; profile_name: string; is_default: boolean }>()
    for (const m of res.models || []) {
      const prev = seen.get(m.model)
      // 同名模型跨配置去重，优先保留带默认标记的条目
      if (!prev || (!prev.is_default && m.is_default)) {
        seen.set(m.model, { model: m.model, profile_id: m.profile_id, profile_name: m.profile_name, is_default: !!m.is_default })
      }
    }
    modelOptions.value = [...seen.values()]
    // 未配置过模型时，默认选中系统配置里的默认模型
    if (!form.models.length && modelOptions.value.length) {
      const def = modelOptions.value.find((m) => m.is_default) || modelOptions.value[0]
      form.models = [def.model]
    }
  } catch {
    /* 未配置 LLM 时忽略 */
  }
  // 知识库候选（类型筛选 + 勾选范围）
  await loadKbLibraries()
}

async function save() {
  if (form.models.length > 10) {
    ElMessage.error('模型最多选择 10 个')
    return
  }
  saving.value = true
  try {
    await updateAgentConfig({ ...form })
    ElMessage.success('智能体配置已保存')
  } catch {
    ElMessage.error('保存失败，请稍后重试')
  } finally {
    saving.value = false
  }
}

// ============ 人格（SOUL）提示词编辑 ============
const promptLoading = ref(false)
const personaSaving = ref(false)
const personaLoaded = ref(false)

const persona = reactive({ content: '', saved: '', custom: false, default: '' })

async function loadPrompts() {
  promptLoading.value = true
  try {
    const p = await getPersona()
    persona.content = p.content; persona.saved = p.content; persona.custom = p.custom; persona.default = p.default
    personaLoaded.value = true
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '人格配置加载失败（请确认 DeerFlow 服务已启动）')
  } finally {
    promptLoading.value = false
  }
}

// 切到「人格 / 技能」Tab 时懒加载
function onTabChange(name: string | number) {
  if (name === 'prompt' && !personaLoaded.value) loadPrompts()
  if (name === 'skills' && !skillsLoaded.value) {
    skillsLoaded.value = true
    loadSkills()
  }
}

async function savePersonaDoc() {
  if (!persona.content.trim()) {
    ElMessage.warning('人格内容不能为空')
    return
  }
  personaSaving.value = true
  try {
    const res = await savePersona({ content: persona.content })
    persona.saved = persona.content
    persona.custom = res.custom
    ElMessage.success('人格已保存，新对话生效')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    personaSaving.value = false
  }
}

async function resetPersonaDoc() {
  try {
    await ElMessageBox.confirm('确定恢复为默认人格模板？当前自定义内容将被清除。', '恢复默认', { type: 'warning' })
    personaSaving.value = true
    const res = await savePersona({ reset: true })
    persona.content = persona.default
    persona.saved = persona.default
    persona.custom = res.custom
    ElMessage.success('已恢复默认人格')
  } catch {
    /* 用户取消 */
  } finally {
    personaSaving.value = false
  }
}

onMounted(load)

// ============ 技能管理（ClawHub/Agent Skills 通用结构） ============
const skills = ref<SkillDoc[]>([])
const skillsLoading = ref(false)
const skillsLoaded = ref(false)
const skillDialogVisible = ref(false)
const skillDialogTitle = ref('新建技能')
const skillFormSaving = ref(false)
const skillForm = reactive({ slug: '', name: '', description: '', content: '', enabled: true })
const importInput = ref<HTMLInputElement | null>(null)
const importing = ref(false)

const SKILL_MD_TEMPLATE = `---
name: my-skill
description: 一句话说明这个技能在什么场景下使用（智能体会据此判断是否调用）
---

# 技能说明

## 适用场景
- 什么情况下应该使用本技能

## 执行步骤
1. 第一步做什么
2. 第二步做什么

## 注意事项
- 约束与规范
`

async function loadSkills() {
  skillsLoading.value = true
  try {
    const res = await listSkills()
    skills.value = res.skills || []
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '技能加载失败（请确认 DeerFlow 服务已启动）')
  } finally {
    skillsLoading.value = false
  }
}

function openCreateSkill() {
  skillDialogTitle.value = '新建技能'
  Object.assign(skillForm, {
    slug: '', name: '', description: '',
    content: SKILL_MD_TEMPLATE, enabled: true,
  })
  skillDialogVisible.value = true
}

function openEditSkill(s: SkillDoc) {
  skillDialogTitle.value = `编辑技能 · ${s.name}`
  Object.assign(skillForm, {
    slug: s.slug,
    name: s.name,
    description: s.description,
    content: s.content,
    enabled: s.enabled,
  })
  skillDialogVisible.value = true
}

async function saveSkillForm() {
  if (!skillForm.name.trim()) {
    ElMessage.warning('技能名称不能为空')
    return
  }
  if (!skillForm.content.trim()) {
    ElMessage.warning('SKILL.md 内容不能为空')
    return
  }
  skillFormSaving.value = true
  try {
    await putSkillDoc({
      name: skillForm.name.trim(),
      slug: skillForm.slug,
      description: skillForm.description.trim(),
      content: skillForm.content,
      enabled: skillForm.enabled,
    })
    ElMessage.success('技能已保存，新对话生效')
    skillDialogVisible.value = false
    await loadSkills()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    skillFormSaving.value = false
  }
}

async function toggleSkill(s: SkillDoc, enabled: boolean) {
  try {
    await putSkillDoc({ name: s.name, description: s.description, content: s.content, enabled })
    s.enabled = enabled
    ElMessage.success(enabled ? '技能已启用' : '技能已停用（内容保留）')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

async function removeSkill(s: SkillDoc) {
  try {
    await ElMessageBox.confirm(`确认删除技能「${s.name}」？删除后不可恢复。`, '删除技能', { type: 'warning' })
    await deleteSkillDoc(s.slug)
    ElMessage.success('技能已删除')
    await loadSkills()
  } catch {
    /* 取消 */
  }
}

function triggerImport() {
  importInput.value?.click()
}

// ============ 机器人头像：本地上传 → canvas 压缩为 128×128 data URL ============
const avatarInput = ref<HTMLInputElement | null>(null)

function triggerAvatarUpload() {
  avatarInput.value?.click()
}

function onAvatarFile(e: Event) {
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
      // 居中裁剪为正方形并缩放到 128×128，避免超大 base64 写入配置
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
      form.bot_avatar = canvas.toDataURL('image/png')
      ElMessage.success('头像已生成，保存配置后生效')
    }
    img.onerror = () => ElMessage.error('图片读取失败')
    img.src = String(reader.result)
  }
  reader.onerror = () => ElMessage.error('图片读取失败')
  reader.readAsDataURL(file)
}

function clearAvatar() {
  form.bot_avatar = ''
}

async function onImportFile(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  importing.value = true
  try {
    const res = await importSkillDoc(file, true)
    if (res.ok === false) {
      ElMessage.error('导入失败')
    } else {
      ElMessage.success(`技能「${res.name || file.name}」导入成功`)
      await loadSkills()
    }
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || '导入失败（需为含 SKILL.md 的 zip 包或 .md 文件）')
  } finally {
    importing.value = false
  }
}
</script>

<template>
  <div class="kge-page kge-page--scroll" v-loading="loading">
    <div class="page-head">
      <div>
        <h2 class="page-title">智能体配置</h2>
        <p class="page-sub">内置智能体策略配置，或通过嵌入代码 / H5 链接接入 HiAgent / Dify / DEAP 平台智能体。</p>
      </div>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </div>

    <!-- 智能体类型切换 -->
    <div class="kind-bar">
      <el-radio-group v-model="agentKind">
        <el-radio-button value="builtin">内置智能体</el-radio-button>
        <el-radio-button value="hiagent">HiAgent智能体</el-radio-button>
        <el-radio-button value="dify">Dify智能体</el-radio-button>
        <el-radio-button value="deap">DEAP智能问答</el-radio-button>
      </el-radio-group>
    </div>

    <el-tabs v-show="agentKind === 'builtin'" v-model="activeTab" class="cfg-tabs" @tab-change="onTabChange">
      <!-- ============ 基础设置 ============ -->
      <el-tab-pane label="基础设置" name="basic">
        <el-collapse v-model="activeCollapse" class="cfg-collapse">
          <!-- 模型 -->
          <el-collapse-item name="model">
            <template #title>
              <span class="card-title"><span class="card-ico ico-model">🧊</span> 模型
                <span class="card-hint">仅可选择系统已接入的模型（最多 10 个）；问答页模型下拉仅展示此列表；点击模型右侧标记设置智能体默认模型（DeerFlow 问答与问答页预选，未设置时跟随列表首个），保存后热更新至服务</span>
              </span>
            </template>
            <el-select
              v-model="form.models"
              multiple
              filterable
              :reserve-keyword="false"
              placeholder="选择系统已接入的模型，最多 10 个"
              class="full-width"
            >
              <el-option
                v-for="m in modelOptions"
                :key="`${m.profile_id}-${m.model}`"
                :label="m.model"
                :value="m.model"
              >
                <span>{{ m.model }}</span>
                <span
                  v-if="form.models.includes(m.model)"
                  :style="{ float: 'right', color: m.model === effectiveDefaultModel ? 'var(--el-color-primary)' : '#c0c4cc', fontSize: '12px', cursor: 'pointer' }"
                  :title="m.model === effectiveDefaultModel ? '智能体默认模型' : '点击设为智能体默认模型'"
                  @click.stop="form.default_model = m.model"
                >{{ m.model === effectiveDefaultModel ? '默认' : '设为默认' }}</span>
                <span v-else-if="m.is_default" style="float: right; color: #909399; font-size: 12px">系统默认</span>
              </el-option>
            </el-select>
          </el-collapse-item>

          <!-- 知识库 -->
          <el-collapse-item name="knowledge">
            <template #title>
              <span class="card-title"><span class="card-ico ico-kb">📚</span> 知识库
                <el-radio-group v-model="form.retrieval_mode" size="small" class="mode-radio" @click.stop>
                  <el-radio-button value="force">强制调用</el-radio-button>
                  <el-radio-button value="smart">智能调用</el-radio-button>
                </el-radio-group>
              </span>
            </template>
            <div class="mode-desc">
              <p><b>强制调用</b>：每个问题（含问候寒暄）都必须先检索知识库再回答（平台注入强制约束），答案严格来自知识库。</p>
              <p><b>智能调用</b>：闲聊问候直接应答；业务问题先检索 Dify 知识库，召回不足时智能体自主补查、再不足兜底钉钉知识库。</p>
            </div>
            <div class="kb-selected">
              <div class="kb-selected-head">
                <span class="kb-selected-hint">接入的知识库参与智能问答检索，开关可临时停用；变更需点「保存配置」生效</span>
                <el-button size="small" @click="openKbDialog">添加知识库</el-button>
              </div>
              <div v-if="kbSelectedLibs.length" class="kb-lib-grid">
                <div
                  v-for="lib in kbSelectedLibs"
                  :key="lib.id"
                  class="kb-lib-card"
                  :class="{ 'is-off': isKbDisabled(lib.id) }"
                >
                  <div class="kb-lib-info">
                    <span class="kb-lib-name" :title="lib.name">{{ lib.name }}</span>
                    <span
                      class="kb-lib-type"
                      :style="isKbDisabled(lib.id) ? undefined : { color: kbTypeMeta(lib).color, background: kbTypeMeta(lib).bg }"
                    >{{ kbTypeLabel(lib) }}</span>
                  </div>
                  <el-switch
                    size="small"
                    :model-value="!isKbDisabled(lib.id)"
                    :aria-label="`${isKbDisabled(lib.id) ? '启用' : '停用'}知识库 ${lib.name}`"
                    @update:model-value="(v: string | number | boolean) => toggleKbEnabled(lib.id, !!v)"
                  />
                  <el-tooltip content="移除知识库" placement="top">
                    <el-icon class="kb-lib-remove" @click="removeKbSelection(lib.id)"><Close /></el-icon>
                  </el-tooltip>
                </div>
              </div>
              <div v-else class="kb-empty-hint">未选择知识库，智能问答将不进行知识检索；点「添加知识库」接入</div>
            </div>
          </el-collapse-item>

          <!-- 工具 -->
          <el-collapse-item name="tools">
            <template #title>
              <span class="card-title"><span class="card-ico ico-tool">🧰</span> 工具
                <span class="card-hint">智能问答可调用的工具，停用后智能体不再使用该能力</span>
              </span>
            </template>
            <div class="tool-list">
              <div v-for="t in toolCatalog" :key="t.key" class="tool-item">
                <div class="tool-info">
                  <span class="tool-name">{{ t.name }}</span>
                  <span class="tool-desc">{{ t.desc }}</span>
                </div>
                <el-switch v-model="form.tools_enabled[t.key]" :aria-label="t.name" />
              </div>
            </div>
          </el-collapse-item>

          <!-- 机器人头像 -->
          <el-collapse-item name="avatar">
            <template #title>
              <span class="card-title"><span class="card-ico ico-greet">🖼️</span> 机器人头像</span>
            </template>
            <div class="avatar-row">
              <div class="avatar-preview">
                <img v-if="form.bot_avatar" :src="form.bot_avatar" alt="机器人头像" />
                <el-icon v-else><MagicStick /></el-icon>
              </div>
              <div class="avatar-actions">
                <el-button size="small" @click="triggerAvatarUpload">上传图片</el-button>
                <el-button v-if="form.bot_avatar" size="small" text type="danger" @click="clearAvatar">恢复默认</el-button>
                <div class="mode-desc"><p>问答页顶部与助手消息显示的头像；自动裁剪压缩为 128×128，保存后生效。</p></div>
              </div>
            </div>
          </el-collapse-item>

          <!-- 对话开场白 -->
          <el-collapse-item name="greeting">
            <template #title>
              <span class="card-title"><span class="card-ico ico-greet">💬</span> 对话开场白
                <el-switch v-model="form.greeting_enabled" class="title-switch" @click.stop />
              </span>
            </template>
            <el-input
              v-model="form.greeting"
              type="textarea"
              :rows="3"
              placeholder="进入问答页时展示的欢迎语"
              class="full-width"
            />
            <div class="sub-label">推荐问题（回车添加，最多 6 个）</div>
            <el-select
              v-model="form.suggested_questions"
              multiple
              filterable
              allow-create
              default-first-option
              :reserve-keyword="false"
              placeholder="输入推荐问题后回车"
              class="full-width"
            />
          </el-collapse-item>

          <!-- 下一步问题建议 -->
          <el-collapse-item name="followup">
            <template #title>
              <span class="card-title"><span class="card-ico ico-follow">👍</span> 下一步问题建议
                <el-switch v-model="form.follow_up_enabled" class="title-switch" @click.stop />
              </span>
            </template>
            <div class="mode-desc">
              <p>完整回答后提供查看原文依据的追问入口，点击即可提问。</p>
            </div>
          </el-collapse-item>
        </el-collapse>
      </el-tab-pane>

      <!-- ============ 高级设置 ============ -->
      <el-tab-pane label="高级设置" name="advanced">
        <el-collapse v-model="activeCollapse" class="cfg-collapse">
          <!-- 任务规划 -->
          <el-collapse-item name="planning">
            <template #title>
              <span class="card-title"><span class="card-ico ico-plan">📋</span> 任务规划
                <el-switch v-model="form.planning_enabled" class="title-switch" @click.stop />
              </span>
            </template>
            <div class="mode-desc">
              <p>开启后 DeerFlow 智能体对复杂问题先生成任务计划（TodoList）再逐步检索执行；关闭后直接检索作答。检索轮数上限见「超参维护」。</p>
            </div>
          </el-collapse-item>

          <!-- 长期记忆 -->
          <el-collapse-item name="memory">
            <template #title>
              <span class="card-title"><span class="card-ico ico-mem">🧠</span> 会话上下文
                <el-switch v-model="form.long_memory_enabled" class="title-switch" @click.stop />
              </span>
            </template>
            <div class="mode-desc">
              <p>开启后同一会话共享 DeerFlow 会话记忆（按会话隔离），用于理解「它/那个/上一条」等指代；关闭后每轮问答使用独立线程，不携带上文。</p>
            </div>
          </el-collapse-item>

          <!-- 超参维护 -->
          <el-collapse-item name="hyper">
            <template #title>
              <span class="card-title"><span class="card-ico ico-hyper">&lt;/&gt;</span> 超参维护</span>
            </template>
            <div class="hyper-grid">
              <div class="hyper-item">
                <div class="hyper-label">事实生成温度 <b>0（固定）</b></div>
                <el-slider :model-value="0" :min="0" :max="2" :step="0.1" disabled />
              </div>
              <div class="hyper-item">
                <div class="hyper-label">Top-P <b>{{ form.top_p.toFixed(2) }}</b></div>
                <el-slider v-model="form.top_p" :min="0.1" :max="1" :step="0.05" />
              </div>
              <div class="hyper-item">
                <div class="hyper-label">召回条数 Top-K <b>{{ form.top_k }}</b></div>
                <el-slider v-model="form.top_k" :min="1" :max="20" :step="1" />
              </div>
              <div class="hyper-item">
                <div class="hyper-label">企业知识检索轮数 <b>{{ form.max_retrieval_rounds }}</b></div>
                <el-slider v-model="form.max_retrieval_rounds" :min="1" :max="2" :step="1" />
              </div>
              <div class="hyper-item hyper-item--input">
                <div class="hyper-label">单次生成 / 核验 Tokens 上限</div>
                <el-input-number v-model="form.max_tokens" :min="3000" :max="8192" :step="256" />
              </div>
            </div>
            <div class="mode-desc">
              <p>Top-P 与 Tokens 上限保存后热更新至 DeerFlow 服务、新对话生效；召回条数与检索轮数逐问即时生效（单轮 knowledge_search 调用上限 = 轮数 × 3）。</p>
            </div>
          </el-collapse-item>
        </el-collapse>
      </el-tab-pane>

      <!-- ============ 人格（DeerFlow SOUL 提示词） ============ -->
      <el-tab-pane label="人格" name="prompt">
        <el-alert title="人格（SOUL）即问答智能体（DeerFlow）的系统提示词：保存后在新对话中生效，自定义内容持久化，服务重启不丢失。" type="info" :closable="false" show-icon />
        <div v-loading="promptLoading">
          <!-- 人格 SOUL.md -->
          <div class="prompt-card">
            <div class="prompt-head">
              <div>
                <span class="card-title"><span class="card-ico ico-soul">🎭</span> 智能体人格 · SOUL.md</span>
                <el-tag :type="persona.custom ? 'warning' : 'info'" size="small" class="prompt-tag">
                  {{ persona.custom ? '自定义' : '默认模板' }}
                </el-tag>
              </div>
              <div class="prompt-actions">
                <el-button size="small" :disabled="personaSaving" @click="resetPersonaDoc">恢复默认</el-button>
                <el-button size="small" type="primary" :loading="personaSaving" :disabled="persona.content === persona.saved" @click="savePersonaDoc">保存</el-button>
              </div>
            </div>
            <p class="prompt-desc">
              定义智能体的身份、职责与行为准则（DeerFlow 系统提示词）。例如：双源检索策略、
              答案风格、引用规范。修改后<strong>在问答智能体的新对话中生效</strong>，进行中的会话不受影响；自定义内容持久化，服务重启不丢失。
            </p>
            <el-input
              v-model="persona.content"
              type="textarea"
              :rows="16"
              spellcheck="false"
              class="prompt-editor"
            />
          </div>
        </div>
      </el-tab-pane>

      <!-- ============ 技能（ClawHub 通用 SKILL.md 结构） ============ -->
      <el-tab-pane label="技能" name="skills">
        <div class="skill-page" v-loading="skillsLoading">
          <div class="skill-toolbar">
            <div class="skill-toolbar-hint">
              兼容 ClawHub 通用技能结构：每个技能为一个目录 + <code>SKILL.md</code>（含 name/description frontmatter）。
              问答智能体（DeerFlow）在新对话中按启用状态加载这些技能；问答技能与其他技能统一在此维护。
            </div>
            <div class="skill-toolbar-ops">
              <input
                ref="importInput"
                type="file"
                accept=".zip,.md,.markdown"
                style="display: none"
                @change="onImportFile"
              />
              <el-button :loading="importing" @click="triggerImport">导入技能（.zip / SKILL.md）</el-button>
              <el-button type="primary" @click="openCreateSkill">新建技能</el-button>
            </div>
          </div>

          <div v-if="!skills.length && !skillsLoading" class="skill-empty">
            暂无自定义技能。可点击「导入技能」上传 ClawHub 技能 zip 包或 SKILL.md 文件，
            或点击「新建技能」直接编写 Markdown。
          </div>

          <div v-for="s in skills" :key="s.slug" class="skill-card">
            <div class="skill-card-main">
              <div class="skill-card-title">
                <span class="skill-name">{{ s.name }}</span>
                <el-tag :type="s.enabled ? 'success' : 'info'" size="small">
                  {{ s.enabled ? '已启用' : '已停用' }}
                </el-tag>
              </div>
              <div class="skill-desc">{{ s.description || '（无描述）' }}</div>
            </div>
            <div class="skill-card-ops">
              <el-switch
                :model-value="s.enabled"
                @change="(v: any) => toggleSkill(s, !!v)"
              />
              <el-button size="small" @click="openEditSkill(s)">编辑 MD</el-button>
              <el-button size="small" type="danger" plain @click="removeSkill(s)">删除</el-button>
            </div>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- ============ 外部平台智能体（HiAgent / Dify 嵌入接入） ============ -->
    <input ref="avatarInput" type="file" accept="image/*" style="display: none" @change="onAvatarFile" />
    <div v-if="agentKind !== 'builtin'" class="embed-panel">
      <div class="embed-card">
        <div class="embed-head">
          <div class="embed-head-info">
            <span class="card-title">
              <span class="card-ico" :class="externalMeta.cls">{{ externalMeta.icon }}</span>
              {{ externalMeta.title }}
            </span>
            <el-tag :type="currentExternal.enabled && parsedEmbedUrl ? 'success' : 'info'" size="small">
              {{ currentExternal.enabled && parsedEmbedUrl ? '已启用' : '未启用' }}
            </el-tag>
          </div>
          <el-switch v-model="currentExternal.enabled" :disabled="!parsedEmbedUrl" active-text="启用" />
        </div>
        <p class="embed-desc">{{ externalMeta.desc }}</p>

        <div class="sub-label">嵌入代码（iframe 片段或页面链接）</div>
        <el-input
          v-model="currentExternal.embed_code"
          type="textarea"
          :rows="7"
          spellcheck="false"
          class="embed-editor"
          :placeholder="externalMeta.placeholder"
        />

        <div class="embed-parsed">
          <span class="embed-parsed-label">页面地址（自动解析）</span>
          <el-input
            :model-value="parsedEmbedUrl"
            readonly
            size="small"
            placeholder="粘贴嵌入代码后自动提取"
          />
        </div>
        <div v-if="currentExternal.embed_code && !parsedEmbedUrl" class="embed-error">
          无法识别页面地址：请粘贴完整的 iframe 代码，或直接粘贴以 http(s):// 开头的链接
        </div>

        <div v-if="parsedEmbedUrl" class="embed-preview">
          <div class="embed-preview-head">
            <span class="embed-preview-title">嵌入预览</span>
            <el-link :href="parsedEmbedUrl" target="_blank" type="primary">在新窗口打开</el-link>
          </div>
          <iframe
            :src="parsedEmbedUrl"
            class="embed-frame"
            frameborder="0"
            allow="microphone;clipboard-write"
          />
          <p class="embed-note">预览空白通常是平台禁止被嵌套（X-Frame-Options），请用「在新窗口打开」验证链接有效性；保存后同样以 iframe 方式嵌入。</p>
        </div>
      </div>
    </div>

    <!-- 技能新建/编辑弹窗 -->
    <el-dialog
      v-model="skillDialogVisible"
      :title="skillDialogTitle"
      width="760px"
      :close-on-click-modal="false"
    >
      <el-form label-position="top">
        <el-form-item label="技能名称（frontmatter name，建议英文小写+中划线）">
          <el-input v-model="skillForm.name" placeholder="例如：expense-report-guide" />
        </el-form-item>
        <el-form-item label="技能描述（frontmatter description：智能体据此判断何时使用）">
          <el-input v-model="skillForm.description" placeholder="一句话说明该技能的适用场景" />
        </el-form-item>
        <el-form-item label="SKILL.md 内容（Markdown，可含 frontmatter；缺省由系统补齐）">
          <el-input
            v-model="skillForm.content"
            type="textarea"
            :rows="16"
            spellcheck="false"
            class="prompt-editor"
          />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="skillForm.enabled" />
          <span class="mode-desc" style="margin-left: 10px">停用后内容保留，智能体不加载该技能</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="skillDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="skillFormSaving" @click="saveSkillForm">保存</el-button>
      </template>
    </el-dialog>

    <!-- 知识库选择弹窗：类型筛选（默认文档库）+ 卡片/列表两种视图 + 勾选 -->
    <el-dialog
      v-model="kbDialogVisible"
      title="添加知识库"
      width="876px"
      class="kb-dialog"
      :close-on-click-modal="false"
    >
      <div class="kb-dialog-bar">
        <el-radio-group v-model="kbTypeFilter" size="small">
          <el-radio-button v-for="t in KB_FILTERS" :key="t.value" :value="t.value">{{ t.label }}</el-radio-button>
        </el-radio-group>
        <el-radio-group v-model="kbView" size="small">
          <el-radio-button value="card">卡片</el-radio-button>
          <el-radio-button value="list">列表</el-radio-button>
        </el-radio-group>
        <el-input
          v-model="kbKeyword"
          class="kb-dialog-search"
          placeholder="搜索知识库名称或 ID"
          clearable
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-checkbox
          :model-value="kbSelectAllChecked"
          :indeterminate="kbSelectAllIndeterminate"
          class="kb-dialog-selectall"
          @change="toggleKbSelectAll"
        >全选</el-checkbox>
        <span class="kb-dialog-count">已选 {{ kbDialogSelection.length }} 个</span>
      </div>
      <div class="kb-dialog-body">
        <div v-if="kbLibrariesError" class="kb-scope-empty">知识库加载失败：{{ kbLibrariesError }}</div>
        <template v-else>
          <!-- 卡片视图：一行 3 卡（公共组件 KbPickCard），整卡点选 -->
          <div v-if="kbView === 'card'" class="kb-card-grid">
            <KbPickCard
              v-for="(lib, idx) in kbFilteredLibraries"
              :key="lib.id"
              :lib="lib"
              :selected="isKbDialogSelected(lib.id)"
              :style="{ animationDelay: (Math.min(idx, 11) * 30) + 'ms' }"
              @toggle="toggleKbDialogSelection(lib)"
            />
            <div v-if="!kbFilteredLibraries.length" class="kb-scope-empty">
              暂无{{ kbFilterDef.label }}，可到「知识应用 → 知识库」创建或登记
            </div>
          </div>
          <!-- 列表视图：单列勾选 -->
          <template v-else>
            <el-checkbox-group v-model="kbDialogSelection" class="kb-check-group kb-check-group--list">
              <el-checkbox v-for="lib in kbFilteredLibraries" :key="lib.id" :value="lib.id" :disabled="!lib.enabled">
                {{ lib.name }}
                <el-tag v-if="!lib.enabled" type="info" size="small" class="kb-lib-tag">停用</el-tag>
              </el-checkbox>
            </el-checkbox-group>
            <div v-if="!kbFilteredLibraries.length" class="kb-scope-empty">
              暂无{{ kbFilterDef.label }}，可到「知识应用 → 知识库」创建或登记
            </div>
          </template>
        </template>
      </div>
      <template #footer>
        <el-button @click="kbDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmKbDialog">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.page-title {
  font-size: 20px;
  font-weight: 700;
  color: #1a1a2e;
  margin: 0 0 6px;
}
.page-sub {
  font-size: 13px;
  color: #909399;
  margin: 0;
}
.cfg-tabs :deep(.el-tabs__item) {
  font-size: 15px;
  font-weight: 600;
}
.kind-bar {
  margin-bottom: 14px;
}
.kind-bar :deep(.el-radio-button__inner) {
  font-weight: 600;
}
/* —— 外部平台智能体（嵌入接入）—— */
.embed-panel {
  margin-top: 4px;
}
.embed-card {
  background: #f7f8fa;
  border: 1px solid #eef0f4;
  border-radius: 12px;
  padding: 18px 20px;
}
.embed-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.embed-head-info {
  display: flex;
  align-items: center;
  gap: 10px;
}
.embed-desc {
  margin: 10px 0 4px;
  font-size: 13px;
  line-height: 1.8;
  color: #606266;
}
.embed-editor :deep(textarea) {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.7;
}
.embed-parsed {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
}
.embed-parsed-label {
  flex-shrink: 0;
  font-size: 13px;
  font-weight: 600;
  color: #606266;
}
.embed-error {
  margin-top: 8px;
  font-size: 12.5px;
  color: #f56c6c;
}
.embed-preview {
  margin-top: 16px;
}
.embed-preview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.embed-preview-title {
  font-size: 13px;
  font-weight: 600;
  color: #1a1a2e;
}
.embed-frame {
  display: block;
  width: 100%;
  height: 560px;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #fff;
}
.embed-note {
  margin: 8px 0 0;
  font-size: 12px;
  color: #909399;
}
.cfg-collapse {
  border: none;
}
.cfg-collapse :deep(.el-collapse-item) {
  background: #f7f8fa;
  border: 1px solid #eef0f4;
  border-radius: 12px;
  margin-bottom: 14px;
  padding: 4px 18px;
}
.cfg-collapse :deep(.el-collapse-item__header) {
  background: transparent;
  border: none;
  height: 56px;
  font-size: 15px;
}
.cfg-collapse :deep(.el-collapse-item__wrap) {
  border: none;
  background: transparent;
}
.cfg-collapse :deep(.el-collapse-item__content) {
  padding-bottom: 18px;
}
.card-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
  color: #1a1a2e;
}
.card-ico {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 8px;
  font-size: 15px;
  font-style: normal;
}
.ico-model { background: #e8f0ff; }
.ico-kb { background: #e8f8ee; }
.ico-greet { background: #e8f0ff; }
.ico-follow { background: #fff4e0; }
.ico-plan { background: #eef0ff; }
.ico-mem { background: #f0e8ff; }
.ico-hyper { background: var(--app-blue-50); font-weight: 700; font-size: 13px; color: var(--app-brand-blue); }
.card-hint {
  font-size: 12px;
  font-weight: 400;
  color: #909399;
  margin-left: 4px;
}
.title-switch {
  margin-left: 12px;
}
.mode-radio {
  margin-left: 14px;
}
/* —— 知识库检索范围 —— */
.kb-help-icon {
  margin-left: 6px;
  color: #909399;
  font-size: 15px;
  cursor: help;
}
.kb-selected-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.kb-selected-hint {
  font-size: 12.5px;
  color: #909399;
}
/* 已接入知识库：等大卡片（264×52），一行自适应多列 */
.kb-lib-grid {
  margin-top: 12px;
  display: grid;
  grid-template-columns: repeat(auto-fill, 264px);
  gap: 10px;
}
.kb-lib-card {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 52px;
  padding: 0 12px;
  border: 1px solid #eef0f4;
  border-radius: 8px;
  background: #fff;
  transition: border-color 0.15s, opacity 0.15s;
}
.kb-lib-card:hover {
  border-color: #d6ddf5;
}
.kb-lib-card.is-off {
  background: #fafbfc;
}
.kb-lib-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.kb-lib-name {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
  line-height: 1.3;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kb-lib-card.is-off .kb-lib-name {
  color: #909399;
}
.kb-lib-type {
  flex-shrink: 0;
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  height: 16px;
  padding: 0 6px;
  border-radius: 3px;
  font-size: 10.5px;
  line-height: 1;
  color: #909399;
  background: #f4f4f5;
}
.kb-lib-remove {
  flex-shrink: 0;
  cursor: pointer;
  color: #c0c4cc;
  font-size: 14px;
  transition: color 0.15s;
}
.kb-lib-remove:hover {
  color: #f56c6c;
}
.kb-empty-hint {
  font-size: 12.5px;
  color: #909399;
  margin-top: 12px;
  padding: 14px 16px;
  border: 1px dashed #dcdfe6;
  border-radius: 8px;
  background: #fafbfc;
}
/* —— 知识库选择弹窗 —— */
.kb-dialog-bar {
  display: flex;
  align-items: center;
  gap: 14px;
}
.kb-dialog-search {
  width: 240px;
  margin-left: auto;
  flex-shrink: 0;
}
.kb-dialog-selectall {
  flex-shrink: 0;
}
.kb-dialog-count {
  font-size: 12.5px;
  color: #909399;
  flex-shrink: 0;
}
/* 内容区固定高度（3 行卡片 + 露出下一行边缘提示可滚动）；两种视图同高防跳动 */
.kb-dialog-body {
  height: 556px;
  overflow-y: auto;
  margin-top: 14px;
}
/* 卡片视图：一行 3 卡（876px 弹窗 - 32px 内边距 - 28px 间距 = 每卡 272px 宽 × 148px 高）；
   minmax(0,1fr) 防长内容撑爆列宽，各卡宽度严格一致；卡片样式在公共组件 KbPickCard 内维护 */
.kb-card-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  align-content: start;
}
.kb-check-group {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 4px 18px;
}
.kb-check-group--list {
  grid-template-columns: 1fr;
}
.kb-check-group :deep(.el-checkbox) {
  height: auto;
  min-height: 28px;
  align-items: center;
  margin-right: 0;
  width: 100%;
}
.kb-check-group :deep(.el-checkbox__label) {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kb-lib-tag {
  flex-shrink: 0;
}
.kb-scope-empty {
  padding: 14px 16px;
  font-size: 12.5px;
  color: #909399;
  background: #fff;
  border: 1px dashed #dcdfe6;
  border-radius: 8px;
  line-height: 1.7;
}
.mode-desc {
  color: #606266;
  font-size: 13px;
  line-height: 1.9;
}
.mode-desc p {
  margin: 4px 0;
}
/* 机器人头像配置 */
.avatar-row {
  display: flex;
  align-items: center;
  gap: 20px;
}
.avatar-preview {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  flex-shrink: 0;
  display: grid;
  place-items: center;
  font-size: 28px;
  color: #fff;
  background: linear-gradient(135deg, var(--el-color-primary) 0%, var(--el-color-primary-light-5) 100%);
  overflow: hidden;
}
.avatar-preview img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.avatar-actions {
  display: flex;
  flex-direction: column;
  gap: 10px;
  align-items: flex-start;
}
.full-width {
  width: 100%;
}
.sub-label {
  margin: 14px 0 8px;
  font-size: 13px;
  color: #606266;
  font-weight: 600;
}
.hyper-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px 32px;
}
.hyper-item {
  min-width: 0;
}
.hyper-item--input {
  align-self: end;
}
.hyper-label {
  font-size: 13px;
  color: #303133;
  margin-bottom: 8px;
}
.hyper-label b {
  color: var(--app-brand-blue);
  margin-left: 6px;
}

/* 人格提示词编辑 */
.prompt-card {
  background: #f7f8fa;
  border: 1px solid #eef0f4;
  border-radius: 12px;
  padding: 16px 18px;
  margin-bottom: 14px;
}
.prompt-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.prompt-tag {
  margin-left: 8px;
}
.prompt-actions {
  display: flex;
  gap: 8px;
}
.prompt-desc {
  font-size: 12.5px;
  color: #909399;
  line-height: 1.8;
  margin: 10px 0 12px;
}
.prompt-editor :deep(textarea) {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace !important;
  font-size: 12.5px !important;
  line-height: 1.75 !important;
}
.ico-soul { background: #f3e8ff; }
.ico-tool { background: #eef0ff; }

/* ===== 工具组 ===== */
.tool-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.tool-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 12px;
  border-radius: 8px;
  background: #fff;
  border: 1px solid #eef0f4;
}
.tool-item:hover {
  border-color: #d6ddf5;
}
.tool-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.tool-name {
  font-size: 13.5px;
  font-weight: 600;
  color: #303133;
}
.tool-desc {
  font-size: 12.5px;
  color: #909399;
  line-height: 1.5;
}

/* ===== 技能 ===== */
.skill-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.skill-toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.skill-toolbar-hint {
  flex: 1 1 320px;
  font-size: 12.5px;
  color: #909399;
  line-height: 1.7;
}
.skill-toolbar-hint code {
  background: #f0f2f8;
  padding: 1px 5px;
  border-radius: 4px;
  color: var(--app-brand-blue);
}
.skill-toolbar-ops {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.skill-empty {
  padding: 36px 20px;
  text-align: center;
  font-size: 13px;
  color: #909399;
  background: #f7f8fa;
  border: 1px dashed #dcdfe6;
  border-radius: 12px;
  line-height: 1.9;
}
.skill-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 16px;
  background: #f7f8fa;
  border: 1px solid #eef0f4;
  border-radius: 12px;
}
.skill-card-main {
  min-width: 0;
  flex: 1 1 auto;
}
.skill-card-title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 4px;
}
.skill-name {
  font-size: 14px;
  font-weight: 700;
  color: #1a1a2e;
}
.skill-desc {
  font-size: 12.5px;
  color: #909399;
  line-height: 1.6;
}
.skill-card-ops {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
</style>

<style>
/* 知识库说明 tooltip（popper 挂载于 body，需非 scoped 样式） */
.kb-help-popper {
  max-width: 340px;
}
.kb-help-popper p {
  margin: 4px 0;
  line-height: 1.7;
}
/* 添加知识库弹窗（el-dialog 挂载于 body，需非 scoped 样式）：
   内边距归一化，使 3 列卡片网格（3×272 + 2×14 gap）在 876px 弹窗内刚好铺满 */
.kb-dialog.el-dialog {
  padding: 16px;
}
.kb-dialog .el-dialog__body {
  padding: 4px 16px 0;
}
</style>
