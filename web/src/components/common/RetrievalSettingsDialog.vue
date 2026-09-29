<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Connection, Promotion, Search, RefreshRight } from '@element-plus/icons-vue'
import { listRerankProfiles, type RerankProfile } from '@/api/settings'

export interface RetrievalSettings {
  mode: 'hybrid' | 'vector' | 'fulltext'
  top_k: number
  score_threshold: number
  rerank: boolean
  rerank_model_id: string
  /** 混合检索未开 Rerank 时的语义权重（关键词权重 = 1 - 该值） */
  vector_weight: number
}

const props = defineProps<{
  modelValue: boolean
  settings: RetrievalSettings
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'save', s: RetrievalSettings): void
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

// 本地编辑副本，取消时回滚
const draft = ref<RetrievalSettings>({ ...props.settings })
watch(() => props.modelValue, (v) => { if (v) draft.value = { ...props.settings } })

const rerankProfiles = ref<RerankProfile[]>([])
async function loadRerankProfiles() {
  try { rerankProfiles.value = await listRerankProfiles() } catch { /* ignore */ }
}
watch(() => props.modelValue, (v) => { if (v) loadRerankProfiles() }, { immediate: true })

const strategies = [
  { value: 'hybrid', label: '混合检索', desc: '同时使用向量检索和全文检索两种策略进行召回，推荐在需要对句子理解和语义关联性的场景使用，综合效果更优', icon: Connection },
  { value: 'vector', label: '向量检索', desc: '返回与查询 Query 含义相匹配的文本分段，而不是与查询字面意思相匹配内容。推荐需要对意图相关性场景使用', icon: Promotion },
  { value: 'fulltext', label: '全文检索', desc: '索引文档中的所有词汇，并返回包含这些词汇的文本片段。推荐在需要对关键词精确匹配的场景下使用', icon: Search },
] as const

const keywordWeight = computed(() => +(1 - draft.value.vector_weight).toFixed(2))

function cancel() { visible.value = false }
function save() {
  emit('save', { ...draft.value })
  visible.value = false
}
</script>

<template>
  <el-dialog v-model="visible" title="检索设置" width="min(600px, 92vw)" :close-on-click-modal="false">
    <div class="rsd-body">
      <!-- 检索策略选择 -->
      <div class="rsd-strategies">
        <div
          v-for="s in strategies"
          :key="s.value"
          class="rsd-strategy"
          :class="{ active: draft.mode === s.value }"
          @click="draft.mode = s.value"
        >
          <el-icon class="rsd-strategy-icon"><component :is="s.icon" /></el-icon>
          <div class="rsd-strategy-main">
            <div class="rsd-strategy-label">{{ s.label }}</div>
            <div class="rsd-strategy-desc">{{ s.desc }}</div>
          </div>
          <el-radio :model-value="draft.mode" :value="s.value" :aria-label="s.label" @change="draft.mode = s.value" class="rsd-strategy-radio" />
        </div>
      </div>

      <!-- 检索参数配置（所有模式统一展示） -->
      <div class="rsd-config-panel">
        <div class="rsd-config-title">
          <el-icon><refresh-right /></el-icon>
          <span>检索参数</span>
        </div>

        <!-- Rerank 模型 -->
        <div class="rsd-config-row">
          <div class="rsd-config-label">
            <span>Rerank 模型</span>
            <el-switch v-model="draft.rerank" size="small" style="margin-left: 12px" />
          </div>
          <el-select
            v-model="draft.rerank_model_id"
            class="rsd-config-control"
            placeholder="请选择 Rerank 模型"
            :disabled="!draft.rerank"
            filterable
          >
            <el-option
              v-for="p in rerankProfiles"
              :key="p.id"
              :label="`${p.name}（${p.model}）`"
              :value="p.id"
            />
          </el-select>
        </div>

        <!-- 权重设置（混合检索未开 Rerank 时生效） -->
        <div v-if="draft.mode === 'hybrid' && !draft.rerank" class="rsd-config-row">
          <div class="rsd-config-label"><span>权重设置</span></div>
          <div class="rsd-weight-group">
            <span class="rsd-weight-label">语义 {{ draft.vector_weight.toFixed(2) }}</span>
            <el-slider
              v-model="draft.vector_weight"
              class="rsd-threshold-slider"
              :min="0"
              :max="1"
              :step="0.05"
              :show-tooltip="false"
            />
            <span class="rsd-weight-label">关键词 {{ keywordWeight.toFixed(2) }}</span>
          </div>
        </div>

        <!-- Top K -->
        <div class="rsd-config-row">
          <div class="rsd-config-label"><span>Top K</span></div>
          <el-input-number
            v-model="draft.top_k"
            class="rsd-config-control"
            :min="1"
            :max="50"
            controls-position="right"
          />
        </div>

        <!-- Score 阈值 -->
        <div class="rsd-config-row">
          <div class="rsd-config-label"><span>Score 阈值</span></div>
          <div class="rsd-threshold-group">
            <el-slider
              v-model="draft.score_threshold"
              class="rsd-threshold-slider"
              :min="0"
              :max="1"
              :step="0.01"
              :show-tooltip="false"
            />
            <el-input-number
              v-model="draft.score_threshold"
              :min="0"
              :max="1"
              :step="0.05"
              :precision="2"
              controls-position="right"
            />
          </div>
        </div>
      </div>
    </div>

    <template #footer>
      <el-button @click="cancel">取消</el-button>
      <el-button type="primary" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.rsd-body { display: flex; flex-direction: column; gap: 20px; }

/* 策略选择区 */
.rsd-strategies { display: flex; flex-direction: column; gap: 10px; }
.rsd-strategy {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s ease;
}
.rsd-strategy:hover { border-color: var(--el-color-primary-light-5); }
.rsd-strategy.active {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.rsd-strategy-icon {
  flex-shrink: 0;
  font-size: 22px;
  color: var(--el-color-primary);
  margin-top: 1px;
}
.rsd-strategy.active .rsd-strategy-icon { color: var(--el-color-primary); }
.rsd-strategy-main { flex: 1; min-width: 0; }
.rsd-strategy-label { font-size: 15px; font-weight: 600; color: var(--el-text-color-primary); margin-bottom: 4px; }
.rsd-strategy-desc { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.6; }
.rsd-strategy-radio { margin-left: 8px; }

/* 参数配置区 */
.rsd-config-panel {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 16px;
  background: var(--el-fill-color-lighter);
}
.rsd-config-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin-bottom: 14px;
}
.rsd-config-title .el-icon { color: var(--el-color-primary); }

.rsd-config-row {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 0;
}
.rsd-config-row + .rsd-config-row { border-top: 1px solid var(--el-border-color-lighter); }
.rsd-config-label {
  flex: 0 0 110px;
  display: flex;
  align-items: center;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.rsd-config-control { flex: 1; max-width: 360px; }

.rsd-threshold-group {
  flex: 1;
  max-width: 360px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.rsd-threshold-slider { flex: 1; }
.rsd-weight-group {
  flex: 1;
  max-width: 360px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.rsd-weight-label { flex: none; font-size: 12px; color: var(--el-text-color-secondary); }
</style>
