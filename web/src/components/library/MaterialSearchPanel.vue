<script setup lang="ts">
import { ref, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { searchMaterialImages, type ImageSearchResult, type ImageHit } from '@/api/material-library'
import MaterialImagePicker from './MaterialImagePicker.vue'
import MaterialImageView from './MaterialImageView.vue'
const props = defineProps<{ libraryId: number; enabled: boolean }>()
const emit = defineEmits<{ material: [id: string] }>()
const file = ref<File | null>(null), crop = ref<number[] | null>(null), loading = ref(false), result = ref<ImageSearchResult | null>(null)
const params = reactive({ category: '', specification: '', top_k: 10 })
const selected = ref<ImageHit | null>(null), compare = ref(false)
let sequence = 0
watch(() => props.libraryId, () => { sequence++; result.value = null; selected.value = null; compare.value = false })
function picked(value: File | null, box: number[] | null) { file.value = value; crop.value = box; result.value = null; sequence++ }
async function search() {
  if (!file.value) { ElMessage.warning('请选择查询图片'); return }
  const ticket = ++sequence
  loading.value = true; result.value = null
  try { const response = await searchMaterialImages(props.libraryId, file.value, { ...params, crop: crop.value }); if (ticket === sequence) result.value = response }
  finally { loading.value = false }
}
</script>
<template>
  <div class="material-search">
    <div class="query-panel">
      <MaterialImagePicker @change="picked" />
      <div class="query-options">
        <el-form label-position="top">
          <el-form-item label="物料分类"><el-input v-model="params.category" placeholder="按分类精确筛选，可留空" clearable /></el-form-item>
          <el-form-item label="规格条件"><el-input v-model="params.specification" placeholder="按规格包含内容筛选，可留空" clearable /></el-form-item>
          <el-form-item label="返回物料数"><el-input-number v-model="params.top_k" :min="1" :max="50" /></el-form-item>
          <el-tooltip content="查询图片沿用已入库图片的抠图、模型和标准化版本，不自动加入物料库"><span class="query-note">按入库规则处理查询图</span></el-tooltip>
          <el-button type="primary" :disabled="!file || !enabled" :loading="loading" @click="search">开始检索</el-button>
        </el-form>
      </div>
    </div>
    <el-alert v-if="!enabled" type="warning" title="物料库已停用，请启用后再检索" :closable="false" />
    <div v-loading="loading" class="results">
      <div class="result-title"><span>物料候选</span><span v-if="result" class="secondary">{{ result.hits.length }} 个结果 · {{ result.elapsed_ms }}ms</span></div>
      <el-empty v-if="!result?.hits.length" :description="result ? result.message || '未找到候选物料，请调整分类、规格或查询图片' : '上传照片，查找外观相似的物料'" />
      <div v-else class="cards">
        <article v-for="hit in result.hits" :key="hit.material_id || hit.image_id" class="hit-card">
          <MaterialImageView :library-id="libraryId" :image-id="hit.image_id" :version="hit.run_id" />
          <h4 :title="hit.material?.name || hit.filename">{{ hit.material?.name || hit.filename }}</h4>
          <div class="meta" :title="hit.material?.code">{{ hit.material?.code || '未关联物料' }}</div>
          <div class="meta" :title="hit.material?.specification">{{ hit.material?.specification || '未填写规格' }}</div>
          <div class="hit-actions"><el-tag type="info" size="small">外观相似候选</el-tag><el-button link type="primary" @click="selected = hit; compare = true">对比图片</el-button></div>
        </article>
      </div>
    </div>
    <el-dialog v-model="compare" title="对比候选物料" width="900px" append-to-body>
      <template v-if="selected">
        <div class="compare-grid"><div><p>查询图（标准化后）</p><el-image :src="result?.previews.find(p => p.group_id === selected?.group_id)?.standard" fit="contain" style="width: 100%; height: 280px" /></div><div><p>命中图片</p><MaterialImageView :library-id="libraryId" :image-id="selected.image_id" :version="selected.run_id" style="height: 280px" /></div></div>
        <el-descriptions :column="2" border><el-descriptions-item label="物料名称">{{ selected.material?.name || '未关联物料' }}</el-descriptions-item><el-descriptions-item label="物料编码">{{ selected.material?.code || '—' }}</el-descriptions-item><el-descriptions-item label="规格">{{ selected.material?.specification || '—' }}</el-descriptions-item><el-descriptions-item label="分类">{{ selected.material?.category || '—' }}</el-descriptions-item></el-descriptions>
        <el-collapse class="trace"><el-collapse-item title="检索详情" name="trace"><p>图像模型：{{ selected.component_name }}</p><p>余弦相似度：{{ selected.cosine_score }}；排序方式：各处理版本召回后，按物料去重并进行 RRF 排名融合</p><p>相似分数用于排序，不代表同一料号的概率；请结合规格确认</p></el-collapse-item></el-collapse>
      </template>
      <template #footer><el-button @click="compare = false">关闭</el-button><el-button v-if="selected?.material_id" type="primary" @click="emit('material', selected.material_id); compare = false">查看物料</el-button></template>
    </el-dialog>
  </div>
</template>
<style scoped>
.query-panel { display: grid; grid-template-columns: minmax(320px, 1fr) 300px; gap: 24px; padding: 16px; border: 1px solid var(--el-border-color-light); border-radius: 8px; }
.query-options .el-input { width: 280px; }
.query-note { display: block; color: var(--el-color-info); font-size: 13px; margin-bottom: 12px; }
.results { min-height: 280px; margin-top: 24px; }
.result-title { display: flex; justify-content: space-between; margin-bottom: 16px; }
.secondary, .meta { color: var(--el-text-color-secondary); font-size: 13px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }
.hit-card { border: 1px solid var(--el-border-color-light); border-radius: 8px; padding: 12px; min-width: 0; }
h4 { margin: 12px 0 8px; }
h4, .meta { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meta { margin-bottom: 8px; }
.hit-actions { display: flex; justify-content: space-between; align-items: center; }
.compare-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
.trace { margin-top: 16px; }
@media (max-width: 900px) { .query-panel { grid-template-columns: 1fr; } }
</style>
