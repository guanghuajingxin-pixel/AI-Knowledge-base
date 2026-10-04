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

const rerankProfiles = ref<RerankProfile[]>([])
/** 检索设置只能选择「模型配置」中已生效（默认）的 Rerank 模型 */
const enabledProfiles = computed(() => rerankProfiles.value.filter(p => p.enabled))

async function loadOnOpen() {
  draft.value = { ...props.settings }
  try {
    rerankProfiles.value = await listRerankProfiles()
  } catch { /* ignore */ }
  // 已保存的模型被停用/删除时清空无效选择；仅一个生效模型时自动选中（与开启开关行为一致）
  if (draft.value.rerank_model_id
    && !enabledProfiles.value.some(p => p.id === draft.value.rerank_model_id)) {
    draft.value.rerank_model_id = ''
  }
  if (draft.value.rerank
    && !draft.value.rerank_model_id && enabledProfiles.value.length === 1) {
    draft.value.rerank_model_id = enabledProfiles.value[0].id
  }
}
watch(() => props.modelValue, (v) => { if (v) void loadOnOpen() })

const strategies = [
  { value: 'hybrid', label: '混合检索', desc: '同时使用向量检索和全文检索两种策略进行召回，推荐在需要对句子理解和语义关联性的场景使用，综合效果更优', icon: Connection },
  { value: 'vector', label: '向量检索', desc: '返回与查询 Query 含义相匹配的文本分段，而不是与查询字面意思相匹配内容。推荐需要对意图相关性场景使用', icon: Promotion },
  { value: 'fulltext', label: '全文检索', desc: '索引文档中的所有词汇，并返回包含这些词汇的文本片段。推荐在需要对关键词精确匹配的场景下使用', icon: Search },
] as const

const keywordWeight = computed(() => +(1 - draft.value.vector_weight).toFixed(2))

function onRerankToggle(v: string | number | boolean) {
  draft.value.rerank = !!v
  // 仅有一个生效模型时自动选中，减少一次操作
  if (v && !draft.value.rerank_model_id && enabledProfiles.value.length === 1) {
    draft.value.rerank_model_id = enabledProfiles.value[0].id
  }
}

function cancel() { visible.value = false }
function save() {
  emit('save', { ...draft.value })
  visible.value = false
}
</script>

<template>
  <el-dialog v-model="visible" title="检索设置" width="min(720px, 94vw)" :close-on-click-modal="false">
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

      <!-- 检索参数配置：统一用 el-form / el-form-item，标签宽度与行间距遵循组件规范 -->
      <div class="rsd-config-panel">
        <div class="rsd-config-title">
          <el-icon><refresh-right /></el-icon>
          <span>检索参数</span>
        </div>

        <el-form class="rsd-form" label-width="96px" label-position="right">
          <!-- Rerank 模型 -->
          <el-form-item label="Rerank 模型">
            <div class="rsd-control-stack">
              <div class="rsd-field-row">
                <el-switch
                  :model-value="draft.rerank"
                  aria-label="Rerank 模型开关"
                  @change="onRerankToggle"
                />
                <el-select
                  v-model="draft.rerank_model_id"
                  class="rsd-rerank-select"
                  placeholder="请选择已生效的 Rerank 模型"
                  :disabled="!draft.rerank"
                  filterable
                  clearable
                >
                  <el-option
                    v-for="p in enabledProfiles"
                    :key="p.id"
                    :label="`${p.name}（${p.model}）`"
                    :value="p.id"
                  />
                </el-select>
              </div>
              <el-alert
                v-if="draft.rerank && enabledProfiles.length === 0"
                type="warning"
                show-icon
                :closable="false"
                title="暂无已生效的 Rerank 模型，请先到「系统配置 · 模型配置」中启用"
                class="rsd-alert"
              />
            </div>
          </el-form-item>

          <!-- 权重设置（混合检索未开 Rerank 时生效） -->
          <el-form-item v-if="draft.mode === 'hybrid' && !draft.rerank" label="权重设置">
            <div class="rsd-weight-group">
              <span class="rsd-weight-label">语义 {{ draft.vector_weight.toFixed(2) }}</span>
              <el-slider
                v-model="draft.vector_weight"
                class="rsd-slider"
                :min="0"
                :max="1"
                :step="0.05"
                :show-tooltip="false"
              />
              <span class="rsd-weight-label">关键词 {{ keywordWeight.toFixed(2) }}</span>
            </div>
          </el-form-item>

          <!-- Top K -->
          <el-form-item label="Top K">
            <el-input-number
              v-model="draft.top_k"
              :min="1"
              :max="50"
              controls-position="right"
              aria-label="Top K"
            />
          </el-form-item>

          <!-- Score 阈值 -->
          <el-form-item label="Score 阈值">
            <div class="rsd-threshold-group">
              <el-slider
                v-model="draft.score_threshold"
                class="rsd-slider"
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
                aria-label="Score 阈值"
              />
            </div>
          </el-form-item>
        </el-form>
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

/* 参数配置区：仅保留卡片容器，行结构交给 el-form-item */
.rsd-config-panel {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 16px 20px 4px;
  background: var(--el-fill-color-lighter);
}
.rsd-config-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin-bottom: 8px;
}
.rsd-config-title .el-icon { color: var(--el-color-primary); }
.rsd-form { max-width: 560px; }

.rsd-control-stack { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.rsd-field-row { display: flex; align-items: center; gap: 12px; }
.rsd-rerank-select { width: 360px; max-width: 100%; }
.rsd-alert { width: 100%; }

.rsd-threshold-group {
  width: 360px;
  max-width: 100%;
  display: flex;
  align-items: center;
  gap: 12px;
}
.rsd-weight-group {
  width: 360px;
  max-width: 100%;
  display: flex;
  align-items: center;
  gap: 12px;
}
.rsd-slider { flex: 1; }
.rsd-weight-label { flex: none; font-size: 12px; color: var(--el-text-color-secondary); }
</style>
