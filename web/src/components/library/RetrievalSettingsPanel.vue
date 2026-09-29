<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ArrowDown, Connection, Promotion, Search, QuestionFilled } from '@element-plus/icons-vue'
import { listRerankProfiles, type RerankProfile } from '@/api/settings'
import { defaultRetrievalSettings, type RetrievalSettings } from './index-settings'

/** 检索设置分段组件（知识库级）：检索模式（混合/向量/全文）+ 混合子策略（权重设置/Rerank 模型，
二选一）+ Top K + Score 阈值。保存于库级 engine_config.retrieval，立即生效——
检索测试未显式传参时按此取库级默认。

props.value 传入当前设置（null/缺省用默认值），组件内部自持表单状态；
父级通过 defineExpose 的 getSettings() 读取最终设置。
*/
const props = withDefaults(defineProps<{ value?: RetrievalSettings | null }>(), {value: null})

const collapsed = ref(false)
const form = reactive<RetrievalSettings>(defaultRetrievalSettings())
const rerankProfiles = ref<RerankProfile[]>([])

// Score 阈值开关：关闭 = 0（不过滤），开启恢复上次非零值（本地态，不落库）
const thresholdOn = ref(false)
const lastThreshold = ref(0.3)

function init(s?: RetrievalSettings | null) {
  Object.assign(form, s ? {...defaultRetrievalSettings(), ...s} : defaultRetrievalSettings())
  collapsed.value = false
  thresholdOn.value = form.score_threshold > 0
  lastThreshold.value = form.score_threshold > 0 ? form.score_threshold : 0.3
}

// 每次打开/切换传入新的设置对象即重新初始化
watch(() => props.value, v => init(v), {immediate: true})

onMounted(async () => {
  try { rerankProfiles.value = await listRerankProfiles() } catch { /* 模型列表不可用仅影响下拉选项 */ }
})

const modes = [
  {key: 'hybrid', title: '混合检索', icon: Connection,
   desc: '同时使用向量检索和全文检索两种策略进行召回，综合效果更优，推荐大多数场景使用'},
  {key: 'vector', title: '向量检索', icon: Promotion,
   desc: '返回与查询含义相匹配的文本分段，而不是与查询字面意思相匹配内容，推荐需要对意图相关性场景使用'},
  {key: 'fulltext', title: '全文检索', icon: Search,
   desc: '索引文档中的所有词汇，并返回包含这些词汇的文本片段，推荐在需要对关键词精确匹配的场景下使用'},
] as const

const keywordWeight = computed(() => +(1 - form.vector_weight).toFixed(2))

function onThresholdSwitch(v: string | number | boolean) {
  const on = !!v
  thresholdOn.value = on
  form.score_threshold = on ? lastThreshold.value : 0
}
function onThresholdInput(v: number | number[] | undefined | null) {
  const n = typeof v === 'number' ? v : 0
  form.score_threshold = n
  if (n > 0) lastThreshold.value = n
}

function getSettings(): RetrievalSettings { return {...form} }
defineExpose({getSettings})
</script>

<template>
  <div class="retrieval-settings">
    <div class="section-head" role="button" @click="collapsed = !collapsed">
      <span class="section-bar" />
      <span class="section-title">检索设置</span>
      <span class="section-hint">保存后立即生效，检索测试默认按此执行</span>
      <el-icon class="arrow" :class="{collapsed}"><ArrowDown /></el-icon>
    </div>
    <div v-show="!collapsed" class="section-body">
      <div v-for="m in modes" :key="m.key" class="mode-card" :class="{active: form.mode === m.key}">
        <div class="mode-head" role="radio" :aria-checked="form.mode === m.key" tabindex="0"
             @click="form.mode = m.key"
             @keydown.enter.prevent="form.mode = m.key">
          <span class="mode-icon"><el-icon :size="20"><component :is="m.icon" /></el-icon></span>
          <span class="mode-text">
            <span class="mode-title">{{ m.title }}</span>
            <span class="mode-desc">{{ m.desc }}</span>
          </span>
          <el-radio v-model="form.mode" :value="m.key" class="mode-radio" :aria-label="m.title">{{ '' }}</el-radio>
        </div>

        <!-- 混合检索：权重设置 / Rerank 模型 二选一 -->
        <div v-if="m.key === 'hybrid' && form.mode === 'hybrid'" class="mode-expand">
          <div class="sub-cards">
            <div class="sub-card" :class="{active: !form.rerank}" role="radio" :aria-checked="!form.rerank"
                 tabindex="0" @click="form.rerank = false" @keydown.enter.prevent="form.rerank = false">
              <span class="sub-main">
                <span class="sub-title">权重设置</span>
                <span class="sub-desc">通过调整分配的权重，确定优先语义匹配还是关键词匹配</span>
              </span>
              <el-radio :model-value="!form.rerank" value="on" class="mode-radio" aria-label="权重设置">{{ '' }}</el-radio>
            </div>
            <div class="sub-card" :class="{active: form.rerank}" role="radio" :aria-checked="form.rerank"
                 tabindex="0" @click="form.rerank = true" @keydown.enter.prevent="form.rerank = true">
              <span class="sub-main">
                <span class="sub-title">Rerank 模型</span>
                <span class="sub-desc">根据候选分段与问题的语义匹配度重新排序，改进语义排序结果</span>
              </span>
              <el-radio :model-value="form.rerank" value="on" class="mode-radio" aria-label="Rerank 模型">{{ '' }}</el-radio>
            </div>
          </div>
          <div v-if="!form.rerank" class="weight-row">
            <span class="weight-label">语义 {{ form.vector_weight.toFixed(2) }}</span>
            <el-slider v-model="form.vector_weight" :min="0" :max="1" :step="0.05" :show-tooltip="false"
                       class="weight-slider" aria-label="语义与关键词权重" />
            <span class="weight-label">关键词 {{ keywordWeight.toFixed(2) }}</span>
          </div>
          <div v-else class="rerank-row">
            <el-select v-model="form.rerank_model_id" filterable clearable class="rerank-select" placeholder="选择 Rerank 模型" aria-label="Rerank 模型">
              <el-option v-for="p in rerankProfiles" :key="p.id" :label="`${p.name}（${p.model}）`" :value="p.id" />
            </el-select>
            <span class="rerank-hint">未选择时使用「模型配置」中的全局 Rerank 配置</span>
          </div>
        </div>

        <!-- 向量/全文检索：Rerank 模型开关 -->
        <div v-else-if="m.key !== 'hybrid' && form.mode === m.key" class="mode-expand">
          <div class="exp-row">
            <span class="exp-label">Rerank 模型<el-tooltip content="开启后按候选分段与问题的语义匹配度重排" placement="top"><el-icon class="tip-icon"><QuestionFilled /></el-icon></el-tooltip></span>
            <div class="rerank-inline">
              <el-switch v-model="form.rerank" aria-label="Rerank 模型开关" />
              <el-select v-model="form.rerank_model_id" filterable clearable :disabled="!form.rerank"
                         class="rerank-select" placeholder="选择 Rerank 模型" aria-label="Rerank 模型">
                <el-option v-for="p in rerankProfiles" :key="p.id" :label="`${p.name}（${p.model}）`" :value="p.id" />
              </el-select>
            </div>
          </div>
        </div>
      </div>

      <div class="field-label param-label">检索参数</div>
      <div class="exp-row">
        <span class="exp-label">Top K<el-tooltip content="检索返回的最大分段数" placement="top"><el-icon class="tip-icon"><QuestionFilled /></el-icon></el-tooltip></span>
        <div class="param-group">
          <el-slider v-model="form.top_k" :min="1" :max="50" :show-tooltip="false" class="param-slider" aria-label="Top K" />
          <el-input-number v-model="form.top_k" :min="1" :max="50" size="small" controls-position="right" class="param-num" aria-label="Top K 数值" />
        </div>
      </div>
      <div class="exp-row">
        <span class="exp-label">Score 阈值<el-tooltip content="最终得分低于阈值的分段将被过滤，关闭则不过滤" placement="top"><el-icon class="tip-icon"><QuestionFilled /></el-icon></el-tooltip></span>
        <div class="param-group">
          <el-switch :model-value="thresholdOn" size="small" class="param-switch" aria-label="Score 阈值开关" @update:model-value="onThresholdSwitch" />
          <template v-if="thresholdOn">
            <el-slider :model-value="form.score_threshold" :min="0" :max="1" :step="0.01" :show-tooltip="false"
                       class="param-slider" aria-label="Score 阈值" @update:model-value="onThresholdInput" />
            <el-input-number :model-value="form.score_threshold" :min="0" :max="1" :step="0.05" :precision="2"
                             size="small" controls-position="right" class="param-num" aria-label="Score 阈值数值"
                             @update:model-value="onThresholdInput" />
          </template>
          <span v-else class="param-off">不过滤</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.retrieval-settings { padding-right: 4px; }
.section-head { display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; padding: 2px 0 14px; border-bottom: 1px solid #ebeef5; margin-bottom: 16px; }
.section-bar { width: 4px; height: 16px; border-radius: 2px; background: var(--el-color-primary); }
.section-title { font-size: 15px; font-weight: 600; color: #303133; }
.section-hint { font-size: 12px; color: #909399; }
.arrow { margin-left: 4px; color: #909399; transition: transform .2s; }
.arrow.collapsed { transform: rotate(-90deg); }
.field-label { font-size: 13px; font-weight: 600; color: #303133; margin-bottom: 10px; }
.mode-card { border: 1px solid #dcdfe6; border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; transition: border-color .15s, background .15s; }
.mode-card:hover { border-color: var(--el-color-primary-light-5); }
.mode-card.active { border-color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
.mode-head { display: flex; align-items: center; gap: 12px; cursor: pointer; }
.mode-icon { flex: none; width: 36px; height: 36px; border-radius: 8px; background: #e8f3ff; color: var(--el-color-primary); display: flex; align-items: center; justify-content: center; }
.mode-text { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.mode-title { font-size: 14px; font-weight: 600; color: #303133; }
.mode-desc { font-size: 12px; color: #909399; }
.mode-radio { flex: none; }
.mode-radio :deep(.el-radio__label) { display: none; }
.mode-expand { margin-top: 14px; padding-top: 14px; border-top: 1px solid #dcdfe6; display: flex; flex-direction: column; gap: 12px; }
.sub-cards { display: flex; gap: 12px; flex-wrap: wrap; }
.sub-card { flex: 1; min-width: 240px; display: flex; align-items: center; gap: 10px; border: 1px solid #dcdfe6; border-radius: 8px; padding: 10px 12px; cursor: pointer; transition: border-color .15s, background .15s; background: #fff; }
.sub-card:hover { border-color: var(--el-color-primary-light-5); }
.sub-card.active { border-color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
.sub-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.sub-title { font-size: 13px; font-weight: 600; color: #303133; }
.sub-desc { font-size: 12px; color: #909399; line-height: 18px; }
.weight-row { display: flex; align-items: center; gap: 12px; }
.weight-label { flex: none; font-size: 12px; color: #606266; }
.weight-slider { flex: 1; }
.rerank-row { display: flex; align-items: center; gap: 12px; }
.rerank-select { width: 320px; max-width: 100%; }
.rerank-hint { font-size: 12px; color: #909399; }
.exp-row { display: flex; align-items: center; gap: 12px; }
.exp-label { flex: none; width: 100px; display: inline-flex; align-items: center; justify-content: flex-end; gap: 4px; font-size: 13px; color: #606266; }
.rerank-inline { display: flex; align-items: center; gap: 12px; flex: 1; }
.param-label { margin-top: 4px; }
.param-group { flex: 1; max-width: 440px; display: flex; align-items: center; gap: 12px; }
.param-slider { flex: 1; }
.param-num { width: 110px; }
.param-switch { flex: none; }
.param-off { font-size: 12px; color: #909399; }
.tip-icon { color: #c0c4cc; font-size: 14px; cursor: help; }
</style>
