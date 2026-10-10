<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ArrowDown, CopyDocument, Files, MagicStick, Operation, QuestionFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { TypeRule } from '@/api/document-library'
import { DELIMITER_PRESETS, FILE_TYPES, METHODS, defaultSettings, type IndexSettings, type Strategy } from './index-settings'

/** 索引设置分段组件（共用）：文档设置弹窗与创建/编辑知识库「解析与分段」步骤共用同一份实现，修改自动同步。

props.value 传入当前设置（null/缺省用默认值），组件内部自持表单状态；
父级通过 defineExpose 的 validate()/getSettings() 读取校验结果与最终设置。
scope=document 隐藏「按文件类型」策略（单文档只有一种类型，该策略无意义）。
策略卡片点击后内联展开对应配置（自定义：分段方式/分段标识符/最大长度/重叠度/预处理；
父子分段：父块模式/父块配置/子块配置/预处理；按文件类型：文件类型规则行编辑）。
*/
const props = withDefaults(defineProps<{
  scope?: 'document' | 'library'
  value?: IndexSettings | null
}>(), {scope: 'document', value: null})

const collapsed = ref(false)
const form = reactive<IndexSettings>(defaultSettings())
const rules = ref<(TypeRule & {ext: string})[]>([])

// 分段标识符：预设多选 + 自定义输入，与 form.delimiter（分隔字符集合字符串）双向同步
const delimSel = ref<string[]>([])
const delimCustom = ref('')

// 子分段标识符：预设单选（换行/2个换行/句号等）+ 自定义，与 form.children_delimiter 双向同步
const CHILD_DELIM_PRESETS = [
  {id: 'nl', label: '换行', chars: '\n'},
  {id: 'nl2', label: '2个换行', chars: '\n\n'},
  {id: 'zh_period', label: '中文句号', chars: '。'},
  {id: 'zh_semicolon', label: '中文分号', chars: '；'},
  {id: 'en_period', label: '英文句号', chars: '.'}] as const
const childDelimCustom = ref('')

const childDelimSel = computed<string>({
  get: () => CHILD_DELIM_PRESETS.find(p => p.chars === form.children_delimiter)?.id || 'custom',
  set: v => {
    if (v === 'custom') { form.children_delimiter = ''; childDelimCustom.value = '' }
    else { form.children_delimiter = CHILD_DELIM_PRESETS.find(p => p.id === v)?.chars || '\n'; childDelimCustom.value = '' }
  },
})

function init(s?: IndexSettings | null) {
  const v = s ? {...defaultSettings(), ...s,
    preprocess: {...s.preprocess}, enhancements: {...s.enhancements}} : defaultSettings()
  Object.assign(form, v)
  rules.value = Object.entries(v.type_rules || {}).map(([ext, r]) => ({ext, ...r}))
  collapsed.value = false
  parseDelimiters(form.delimiter)
  childDelimCustom.value = CHILD_DELIM_PRESETS.some(p => p.chars === form.children_delimiter) ? '' : form.children_delimiter
}

// 每次打开/切换传入新的设置对象即重新初始化（弹窗与分步向导均以新对象触发）
watch(() => props.value, v => init(v), {immediate: true})

// 父子分段仅支持通用文档策略（后端校验 enable_children + chunk_method=naive）；
// 选中时若分段标识符仍是默认全集合（换行+句读），自动切到段落级「2个换行」——
// 父块是召回上下文，句级标识符会让父块过碎（用户可自行改回）
watch(() => form.strategy, v => {
  if (v !== 'parent_child') return
  form.method = 'naive'
  if (form.delimiter === '\n。！？；') delimSel.value = ['nl2']  // syncDelimiters → '\n\n'
})

watch(delimSel, syncDelimiters, {deep: true})
watch(delimCustom, syncDelimiters)
watch(childDelimCustom, v => { if (childDelimSel.value === 'custom' && v) form.children_delimiter = v })

function parseDelimiters(s: string) {
  let rest = s || ''
  const sel: string[] = []
  // 换行两级：先长后短（'\n\n' 段落级 / '\n' 行级，序列化互斥可判别，
  // 供父子分段父块切分区分段落与行——Dify fixed_separator 语义）
  if (rest.includes('\n\n')) { sel.push('nl2'); rest = rest.split('\n\n').join('') }
  else if (rest.includes('\n')) { sel.push('nl'); rest = rest.split('\n').join('') }
  for (const p of DELIMITER_PRESETS) {
    if (p.id === 'nl' || p.id === 'nl2') continue
    if (rest.includes(p.chars)) { sel.push(p.id); rest = rest.split(p.chars).join('') }
  }
  delimSel.value = sel
  delimCustom.value = rest
}

function syncDelimiters() {
  const chars: string[] = []
  const push = (c: string) => { if (!chars.includes(c)) chars.push(c) }
  // 段落级（2个换行）与行级（换行）互斥：选了 nl2 不再序列化单 '\n'
  if (delimSel.value.includes('nl2')) push('\n\n')
  else if (delimSel.value.includes('nl')) push('\n')
  for (const p of DELIMITER_PRESETS) {
    if (p.id === 'nl' || p.id === 'nl2') continue
    if (delimSel.value.includes(p.id)) push(p.chars)
  }
  for (const c of delimCustom.value) push(c)
  form.delimiter = chars.join('')
}

interface StrategyCard { key: Exclude<Strategy, 'by_file_type'>; title: string; desc: string }
const cards: StrategyCard[] = [
  {key: 'auto', title: '自动', desc: '自动设置分段与预处理规则'},
  {key: 'custom', title: '自定义', desc: '自定义文本分块模式，检索和召回的是相同的'},
  {key: 'parent_child', title: '父子分段', desc: '使用父子模式时，子块用于检索，父块用作上下文'}]

const enhancements = computed(() => [
  {key: 'include_filename', label: '加入文件名', tip: '检索与召回时在分段内容前附带文档名，便于模型溯源'},
  {key: 'auto_summary', label: '模型补充摘要', tip: '解析时由模型为每个分段生成内容摘要，提升语义检索命中'},
  {key: 'auto_questions', label: '模型补充用户问题', tip: '解析时由模型为每个分段生成候选问题，提升问答类查询命中率'},
  {key: 'image_caption', label: '模型补充图片描述', tip: '解析时由模型生成分段内图片的文字描述'}] as const)

function addRule() {
  rules.value.push({ext: '', strategy: 'auto', method: 'naive', chunk_token_num: 512, delimiter: '\n。！？；', children_delimiter: '\n'})
}

/** 校验分段设置（自定义/父子分段-段落模式必填分段标识符；文件类型规则不允许重复扩展名），失败时 Message 提示并返回 false。 */
function validate(): boolean {
  if (form.strategy === 'custom' || (form.strategy === 'parent_child' && form.parent_mode === 'paragraph')) {
    if (!form.delimiter) { ElMessage.warning('请选择分段标识符'); return false }
    if (delimSel.value.includes('custom') && !delimCustom.value.trim()) { ElMessage.warning('请输入自定义分段标识符'); return false }
  }
  if (form.strategy === 'parent_child' && childDelimSel.value === 'custom' && !childDelimCustom.value.trim()) {
    ElMessage.warning('请输入子分段标识符'); return false
  }
  if (form.strategy === 'by_file_type') {
    const seen = new Set<string>()
    for (const r of rules.value) {
      const ext = r.ext.trim().toLowerCase().replace('.', '')
      if (!ext) { ElMessage.warning('请选择文件类型'); return false }
      if (seen.has(ext)) { ElMessage.warning(`文件类型 ${ext} 重复配置`); return false }
      seen.add(ext)
    }
  }
  return true
}

/** 输出最终设置：表单快照 + type_rules 序列化（扩展名小写、去点号）。 */
function getSettings(): IndexSettings {
  const type_rules: Record<string, TypeRule> = {}
  for (const {ext, ...rule} of rules.value) {
    if (ext.trim()) type_rules[ext.trim().toLowerCase().replace('.', '')] = rule
  }
  return {...form, preprocess: {...form.preprocess}, enhancements: {...form.enhancements}, type_rules}
}

defineExpose({validate, getSettings})
</script>

<template>
  <div class="index-settings">
    <div class="section-head" role="button" @click="collapsed = !collapsed">
      <span class="section-bar" />
      <span class="section-title">索引设置</span>
      <el-icon class="arrow" :class="{collapsed}"><ArrowDown /></el-icon>
    </div>
    <div v-show="!collapsed" class="section-body">
      <div class="field-label">分段策略</div>

      <div v-for="card in cards" :key="card.key"
           class="strategy-card" :class="{active: form.strategy === card.key}">
        <div class="strategy-head" role="radio" :aria-checked="form.strategy === card.key" tabindex="0"
             @click="form.strategy = card.key"
             @keydown.enter.prevent="form.strategy = card.key">
          <span class="strategy-icon"><el-icon :size="20"><component :is="card.key === 'auto' ? MagicStick : card.key === 'custom' ? Operation : CopyDocument" /></el-icon></span>
          <span class="strategy-text">
            <span class="strategy-title">{{ card.title }}</span>
            <span class="strategy-desc">{{ card.desc }}</span>
          </span>
          <el-radio v-model="form.strategy" :value="card.key" class="strategy-radio" :aria-label="card.title">{{ '' }}</el-radio>
        </div>

        <!-- 自定义：分段方式 / 分段标识符 / 分段最大长度 / 分段重叠度 / 文本预处理规则 -->
        <div v-if="card.key === 'custom' && form.strategy === 'custom'" class="strategy-expand">
          <div class="exp-row">
            <span class="exp-label">分段方式</span>
            <el-select v-model="form.method" class="exp-field">
              <el-option v-for="m in METHODS" :key="m.value" :label="m.label" :value="m.value" />
            </el-select>
          </div>
          <div class="exp-row">
            <span class="exp-label"><span class="req">*</span>分段标识符<el-tooltip content="用于切句的分隔符，可多选" placement="top"><el-icon class="tip-icon"><QuestionFilled /></el-icon></el-tooltip></span>
            <el-select v-model="delimSel" multiple class="exp-field" placeholder="请选择分段标识符">
              <el-option v-for="p in DELIMITER_PRESETS" :key="p.id" :label="p.label" :value="p.id" />
              <el-option label="自定义" value="custom" />
            </el-select>
          </div>
          <div v-if="delimSel.includes('custom')" class="exp-row">
            <span class="exp-label"><span class="req">*</span>自定义</span>
            <el-input v-model="delimCustom" class="exp-field" placeholder="请输入自定义分段标识符" />
          </div>
          <div class="exp-row">
            <span class="exp-label"><span class="req">*</span>分段最大长度<el-tooltip content="每个分段的目标长度，按 Token 数滚动窗口聚合" placement="top"><el-icon class="tip-icon"><QuestionFilled /></el-icon></el-tooltip></span>
            <el-input-number v-model="form.chunk_token_num" :min="1" :max="2048" controls-position="right" />
          </div>
          <div class="exp-row">
            <span class="exp-label">分段重叠度<el-tooltip content="相邻分段重复携带的 Token 数，提升跨分段语义连续性" placement="top"><el-icon class="tip-icon"><QuestionFilled /></el-icon></el-tooltip></span>
            <el-input-number v-model="form.overlap" :min="0" :max="1024" controls-position="right" />
          </div>
          <div class="exp-row wrap">
            <span class="exp-label">文本预处理规则</span>
            <div class="exp-checks">
              <el-checkbox v-model="form.preprocess.replace_whitespace">替换掉连续的空格、换行符和制表符</el-checkbox>
              <el-checkbox v-model="form.preprocess.remove_urls_emails">删除所有URL和电子邮箱地址</el-checkbox>
            </div>
          </div>
        </div>

        <!-- 父子分段：父块模式 + 父块配置 + 子块配置 + 预处理 -->
        <div v-if="card.key === 'parent_child' && form.strategy === 'parent_child'" class="strategy-expand">
          <div class="exp-row">
            <span class="exp-label">父块模式<el-tooltip content="父块作为召回上下文的组织方式：段落=按分隔符切出的分段作父块；全文=整篇文档作为单个父块" placement="top"><el-icon class="tip-icon"><QuestionFilled /></el-icon></el-tooltip></span>
            <el-radio-group v-model="form.parent_mode" aria-label="父块模式">
              <el-radio value="paragraph">段落</el-radio>
              <el-radio value="fulltext">全文</el-radio>
            </el-radio-group>
          </div>
          <template v-if="form.parent_mode === 'paragraph'">
            <div class="exp-row">
              <span class="exp-label"><span class="req">*</span>分段标识符<el-tooltip content="父块按分隔符与最大长度切分，切出的段落作为父块" placement="top"><el-icon class="tip-icon"><QuestionFilled /></el-icon></el-tooltip></span>
              <el-select v-model="delimSel" multiple class="exp-field" placeholder="请选择分段标识符">
                <el-option v-for="p in DELIMITER_PRESETS" :key="p.id" :label="p.label" :value="p.id" />
                <el-option label="自定义" value="custom" />
              </el-select>
            </div>
            <div v-if="delimSel.includes('custom')" class="exp-row">
              <span class="exp-label"><span class="req">*</span>自定义</span>
              <el-input v-model="delimCustom" class="exp-field" placeholder="请输入自定义分段标识符" />
            </div>
            <div class="exp-row">
              <span class="exp-label"><span class="req">*</span>分段最大长度<el-tooltip content="父块的目标长度，按 Token 数滚动窗口聚合" placement="top"><el-icon class="tip-icon"><QuestionFilled /></el-icon></el-tooltip></span>
              <el-input-number v-model="form.chunk_token_num" :min="1" :max="2048" controls-position="right" />
            </div>
          </template>
          <div v-else class="exp-hint">整个文档将作为父块提供完整上下文，超过 10000 Token 自动截断</div>
          <div class="exp-sub-title">子块用于检索</div>
          <div class="exp-row">
            <span class="exp-label">子分段标识符<el-tooltip content="父块内按该分隔符切出子块，子块用于向量检索" placement="top"><el-icon class="tip-icon"><QuestionFilled /></el-icon></el-tooltip></span>
            <el-select v-model="childDelimSel" class="exp-field" aria-label="子分段标识符">
              <el-option v-for="p in CHILD_DELIM_PRESETS" :key="p.id" :label="p.label" :value="p.id" />
              <el-option label="自定义" value="custom" />
            </el-select>
          </div>
          <div v-if="childDelimSel === 'custom'" class="exp-row">
            <span class="exp-label"><span class="req">*</span>自定义</span>
            <el-input v-model="childDelimCustom" class="exp-field" placeholder="请输入子分段标识符" />
          </div>
          <div class="exp-row">
            <span class="exp-label">子分段最大长度<el-tooltip content="相邻短子块会合并到该长度，超长子块自动细分；表格保持完整不切断" placement="top"><el-icon class="tip-icon"><QuestionFilled /></el-icon></el-tooltip></span>
            <el-input-number v-model="form.children_chunk_token_num" :min="1" :max="2048" controls-position="right" />
          </div>
          <div class="exp-row wrap">
            <span class="exp-label">文本预处理规则</span>
            <div class="exp-checks">
              <el-checkbox v-model="form.preprocess.replace_whitespace">替换掉连续的空格、换行符和制表符</el-checkbox>
              <el-checkbox v-model="form.preprocess.remove_urls_emails">删除所有URL和电子邮箱地址</el-checkbox>
            </div>
          </div>
        </div>
      </div>

      <div v-if="scope === 'library'" class="strategy-card" :class="{active: form.strategy === 'by_file_type'}">
        <div class="strategy-head" role="radio" :aria-checked="form.strategy === 'by_file_type'" tabindex="0"
             @click="form.strategy = 'by_file_type'"
             @keydown.enter.prevent="form.strategy = 'by_file_type'">
          <span class="strategy-icon"><el-icon :size="20"><Files /></el-icon></span>
          <span class="strategy-text">
            <span class="strategy-title">按文件类型</span>
            <span class="strategy-desc">指定类型文件将通过该策略解析，其它类型使用【自动】策略</span>
          </span>
          <el-radio v-model="form.strategy" value="by_file_type" class="strategy-radio" aria-label="按文件类型">{{ '' }}</el-radio>
        </div>
        <div v-if="form.strategy === 'by_file_type'" class="strategy-expand">
          <div v-for="(rule, i) in rules" :key="i" class="type-rule">
            <el-select v-model="rule.ext" class="rule-ext" filterable allow-create placeholder="选择文件类型" aria-label="文件类型">
              <el-option v-for="t in FILE_TYPES" :key="t.ext" :label="t.label" :value="t.ext" />
            </el-select>
            <el-select v-model="rule.strategy" class="rule-strategy" aria-label="分段策略" @change="v => { if (v === 'parent_child') rule.method = 'naive' }">
              <el-option label="自动" value="auto" />
              <el-option label="自定义" value="custom" />
              <el-option label="父子分段" value="parent_child" />
            </el-select>
            <template v-if="rule.strategy === 'custom'">
              <el-select v-model="rule.method" class="rule-method" aria-label="分段方式">
                <el-option v-for="m in METHODS" :key="m.value" :label="m.label" :value="m.value" />
              </el-select>
              <el-input-number v-model="rule.chunk_token_num" :min="1" :max="2048" class="rule-num" controls-position="right" aria-label="分段最大长度" />
            </template>
            <el-input-number v-else-if="rule.strategy === 'parent_child'" v-model="rule.chunk_token_num" :min="1" :max="2048" class="rule-num" controls-position="right" aria-label="父块最大长度" />
            <el-button link type="danger" @click="rules.splice(i, 1)">移除</el-button>
          </div>
          <el-button link type="primary" @click="addRule">+ 添加文件类型</el-button>
          <div class="rule-hint">解析时按文档扩展名命中规则；未命中的文件类型使用【自动】策略</div>
        </div>
      </div>

      <div class="field-label enhance-label">检索内容增强</div>
      <div class="enhance-grid">
        <el-checkbox v-for="item in enhancements" :key="item.key" v-model="form.enhancements[item.key]">
          {{ item.label }}
          <el-tooltip :content="item.tip" placement="top"><el-icon class="tip-icon"><QuestionFilled /></el-icon></el-tooltip>
        </el-checkbox>
      </div>
    </div>
  </div>
</template>

<style scoped>
.index-settings { padding-right: 4px; }
.section-head { display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; padding: 2px 0 14px; border-bottom: 1px solid #ebeef5; margin-bottom: 16px; }
.section-bar { width: 4px; height: 16px; border-radius: 2px; background: var(--el-color-primary); }
.section-title { font-size: 15px; font-weight: 600; color: #303133; }
.arrow { margin-left: 4px; color: #909399; transition: transform .2s; }
.arrow.collapsed { transform: rotate(-90deg); }
.field-label { font-size: 13px; font-weight: 600; color: #303133; margin-bottom: 10px; }
.strategy-card { border: 1px solid #dcdfe6; border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; transition: border-color .15s, background .15s; }
.strategy-card:hover { border-color: var(--el-color-primary-light-5); }
.strategy-card.active { border-color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
.strategy-head { display: flex; align-items: center; gap: 12px; cursor: pointer; }
.strategy-icon { flex: none; width: 36px; height: 36px; border-radius: 8px; background: #e8f3ff; color: var(--el-color-primary); display: flex; align-items: center; justify-content: center; }
.strategy-text { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.strategy-title { font-size: 14px; font-weight: 600; color: #303133; }
.strategy-desc { font-size: 12px; color: #909399; }
.strategy-radio { flex: none; }
.strategy-radio :deep(.el-radio__label) { display: none; }
.strategy-expand { margin-top: 14px; padding-top: 14px; border-top: 1px solid #dcdfe6; display: flex; flex-direction: column; gap: 12px; }
.exp-row { display: flex; align-items: center; gap: 12px; }
.exp-row.wrap { align-items: flex-start; }
.exp-row.wrap .exp-label { padding-top: 1px; }
.exp-label { flex: none; width: 120px; display: inline-flex; align-items: center; justify-content: flex-end; gap: 4px; font-size: 13px; color: #606266; }
.exp-label .tip-icon { margin-left: -2px; }
.req { color: var(--el-color-danger); }
.exp-field { flex: 1; max-width: 440px; }
.exp-checks { display: flex; flex-wrap: wrap; gap: 4px 16px; }
.exp-checks :deep(.el-checkbox) { margin-right: 0; height: auto; }
.type-rule { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.rule-ext { width: 160px; }
.rule-strategy { width: 110px; }
.rule-method { width: 130px; }
.rule-num { width: 120px; }
.rule-hint { font-size: 12px; color: #909399; }
.exp-sub-title { font-size: 13px; font-weight: 600; color: #303133; margin-top: 2px; }
.exp-hint { font-size: 12px; color: #909399; line-height: 22px; padding: 2px 0; }
.enhance-label { margin-top: 18px; }
.enhance-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 24px; }
.enhance-grid :deep(.el-checkbox) { margin-right: 0; height: auto; }
.tip-icon { color: #c0c4cc; font-size: 14px; cursor: help; }
</style>
