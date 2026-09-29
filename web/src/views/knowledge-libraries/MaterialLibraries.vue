<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { MoreFilled, Setting } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import * as api from '@/api/material-library'
import ImageComponentsPanel from '@/components/library/ImageComponentsPanel.vue'
import MaterialImagePicker from '@/components/library/MaterialImagePicker.vue'
import MaterialImageView from '@/components/library/MaterialImageView.vue'
import MaterialSearchPanel from '@/components/library/MaterialSearchPanel.vue'
const canEdit = computed(() => ['super_admin', 'admin', 'editor'].includes(useUserStore().userInfo?.role || ''))
const libraries = ref<api.MaterialLibrary[]>([]), selectedId = ref<number | null>(null), components = ref<api.ImageComponent[]>([])
const selected = computed(() => libraries.value.find(l => l.id === selectedId.value))
const loading = ref(false), saving = ref(false), tab = ref('materials'), componentsDialog = ref(false)
const embeddings = computed(() => components.value.filter(c => c.kind === 'embedding'))
const removers = computed(() => components.value.filter(c => c.kind === 'remover'))
const libDialog = ref(false), editLibId = ref<number | null>(null)
const libForm = reactive({ name: '', description: '', enabled: true, config: { embedding_id: null as string | null, remover_id: null as string | null, preprocess: true } })
const materials = ref<api.Material[]>([]), total = ref(0), query = ref(''), page = ref(1)
const materialDialog = ref(false), editMaterialId = ref<string | null>(null)
const emptyMaterial = (): api.MaterialInput => ({ code: '', name: '', specification: '', category: '', tags: [], description: '', enabled: true })
const matForm = reactive(emptyMaterial())
const images = ref<api.MaterialImage[]>([]), imageTotal = ref(0), imagePage = ref(1), imageFilter = ref<string>(''), filterName = ref('')
const imageLoading = ref(false), uploadDialog = ref(false), uploadFile = ref<File | null>(null), uploadCrop = ref<number[] | null>(null)
const uploadPreprocess = ref(true), uploadMaterialId = ref<string | null>(null), materialOptions = ref<api.Material[]>([])
const detailImage = ref<api.MaterialImage | null>(null), detailDialog = ref(false), retryDialog = ref(false), retryPreprocess = ref(true), useLibraryConfig = ref(false)
const bindDialog = ref(false), binding = ref<api.MaterialImage | null>(null), bindId = ref<string | null>(null)
let listSequence = 0, imageSequence = 0
const stateLabels: Record<string, string> = { PENDING: '等待处理', PROCESSING: '预处理中', INDEXING: '向量化中', RETRYING: '等待重试', COMPLETED: '已完成', FAILED: '处理失败', DELETING: '清理未完成' }
function stateType(status: string): 'success' | 'warning' | 'danger' | 'info' { return status === 'COMPLETED' ? 'success' : ['FAILED', 'DELETING'].includes(status) ? 'danger' : 'warning' }
async function loadLibraries() { libraries.value = await api.listMaterialLibraries(); if (!libraries.value.some(l => l.id === selectedId.value)) selectedId.value = libraries.value[0]?.id || null }
async function loadComponents() { components.value = await api.listImageComponents() }
async function loadMaterials() {
  if (!selectedId.value) return
  const ticket = ++listSequence; loading.value = true
  try { const data = await api.listMaterials(selectedId.value, { q: query.value, page: page.value }); if (ticket === listSequence) { materials.value = data.items; total.value = data.total } }
  finally { if (ticket === listSequence) loading.value = false }
}
async function loadImages() {
  if (!selectedId.value) return
  const ticket = ++imageSequence; imageLoading.value = true
  try {
    const data = await api.listMaterialImages(selectedId.value, { page: imagePage.value, page_size: 12, material_id: imageFilter.value && imageFilter.value !== 'unassigned' ? imageFilter.value : undefined, unassigned: imageFilter.value === 'unassigned' })
    if (ticket === imageSequence) { images.value = data.items; imageTotal.value = data.total; if (detailImage.value) detailImage.value = data.items.find(i => i.id === detailImage.value?.id) || detailImage.value }
  } finally { if (ticket === imageSequence) imageLoading.value = false }
}
watch(selectedId, () => { listSequence++; imageSequence++; materials.value = []; images.value = []; page.value = 1; imagePage.value = 1; query.value = ''; imageFilter.value = ''; filterName.value = ''; detailDialog.value = false; void loadMaterials(); void loadImages() })
watch([page, query], () => { void loadMaterials() })
watch([imagePage, imageFilter], () => { void loadImages() })
onMounted(async () => { await Promise.all([loadLibraries(), loadComponents()]) })
const timer = setInterval(() => { if (tab.value === 'images' && !imageLoading.value && images.value.some(i => ['PENDING', 'PROCESSING', 'INDEXING', 'RETRYING'].includes(i.status))) void loadImages().catch(() => {}) }, 5000)
onBeforeUnmount(() => { clearInterval(timer); listSequence++; imageSequence++ })
function openLibrary(row?: api.MaterialLibrary) {
  editLibId.value = row?.id || null
  Object.assign(libForm, { name: row?.name || '', description: row?.description || '', enabled: row?.enabled ?? true, config: { embedding_id: row?.engine_config.embedding_id || null, remover_id: row?.engine_config.remover_id || null, preprocess: row?.engine_config.preprocess ?? true } })
  libDialog.value = true
}
async function saveLibrary() {
  if (!libForm.name.trim()) { ElMessage.warning('填写物料库名称'); return }
  saving.value = true
  try { const row = await api.saveMaterialLibrary(editLibId.value, libForm); libDialog.value = false; await loadLibraries(); selectedId.value = row.id; ElMessage.success('已保存物料库') } finally { saving.value = false }
}
async function removeLibrary() {
  if (!selected.value) return
  try { await ElMessageBox.confirm(`删除物料库「${selected.value.name}」？需要先清空库内图片及物料`, '删除物料库', { type: 'warning' }); await api.deleteMaterialLibrary(selected.value.id); await loadLibraries(); ElMessage.success('已删除物料库') } catch { /* cancellation */ }
}
function openMaterial(row?: api.Material) { editMaterialId.value = row?.id || null; Object.assign(matForm, row ? { code: row.code, name: row.name, specification: row.specification, category: row.category, tags: [...row.tags], description: row.description, enabled: row.enabled } : emptyMaterial()); materialDialog.value = true }
async function saveMaterial() {
  if (!selectedId.value || !matForm.name.trim() || !matForm.code.trim()) { ElMessage.warning('填写物料编码与名称'); return }
  saving.value = true
  try { await api.saveMaterial(selectedId.value, editMaterialId.value, matForm); materialDialog.value = false; await loadMaterials(); ElMessage.success('已保存物料') } finally { saving.value = false }
}
async function materialCommand(command: string, row: api.Material) {
  if (!selectedId.value) return
  try {
    if (command === 'delete') { await ElMessageBox.confirm(`删除物料「${row.name}」？请先删除其关联图片`, '删除物料', { type: 'warning' }); await api.deleteMaterial(selectedId.value, row.id) }
    else { const { code, name, specification, category, tags, description } = row; await api.saveMaterial(selectedId.value, row.id, { code, name, specification, category, tags, description, enabled: !row.enabled }) }
    await loadMaterials(); ElMessage.success('操作成功')
  } catch { /* cancellation */ }
}
async function showMaterialImages(id: string) { if (!selectedId.value) return; const mat = await api.getMaterial(selectedId.value, id); filterName.value = `${mat.code} · ${mat.name}`; imagePage.value = 1; imageFilter.value = id; tab.value = 'images'; await loadImages() }
async function searchMaterialOptions(q = '') { if (selectedId.value) materialOptions.value = (await api.listMaterials(selectedId.value, { q, page_size: 100 })).items }
async function openUpload() {
  if (!selected.value?.engine_config.embedding_id) { ElMessage.warning('请先在库设置中选择图像向量组件'); return }
  uploadFile.value = null; uploadCrop.value = null; uploadPreprocess.value = selected.value.engine_config.preprocess
  uploadMaterialId.value = imageFilter.value && imageFilter.value !== 'unassigned' ? imageFilter.value : null
  await searchMaterialOptions(); uploadDialog.value = true
}
async function upload() {
  if (!uploadFile.value || !selectedId.value) return
  if (uploadPreprocess.value && !selected.value?.engine_config.remover_id) { ElMessage.warning('开启图片预加工需要在库设置中选择抠图组件'); return }
  saving.value = true
  try {
    const row = await api.uploadMaterialImage(selectedId.value, uploadFile.value, uploadMaterialId.value, uploadPreprocess.value, uploadCrop.value)
    uploadDialog.value = false; tab.value = 'images'; imageFilter.value = uploadMaterialId.value || ''; imagePage.value = 1
    await Promise.all([loadImages(), loadMaterials()]); row.status === 'FAILED' ? ElMessage.warning(row.message) : ElMessage.success('已上传，后台开始处理')
  } finally { saving.value = false }
}
function retry(row: api.MaterialImage) { detailImage.value = row; retryPreprocess.value = row.preprocess; useLibraryConfig.value = false; retryDialog.value = true }
async function runRetry() {
  if (!detailImage.value || !selectedId.value) return
  saving.value = true
  try { const result = await api.retryMaterialImage(selectedId.value, detailImage.value.id, { preprocess: retryPreprocess.value, use_library_config: useLibraryConfig.value }); retryDialog.value = false; await loadImages(); result.status === 'FAILED' ? ElMessage.warning(result.message) : ElMessage.success('已提交重新处理') } finally { saving.value = false }
}
async function imageCommand(command: string, row: api.MaterialImage) {
  if (!selectedId.value) return
  try {
    if (command === 'delete') { await ElMessageBox.confirm(`删除图片「${row.filename}」？原图、处理产物及索引将被删除`, '删除图片', { type: 'warning' }); await api.deleteMaterialImage(selectedId.value, row.id) }
    if (command === 'toggle') await api.editMaterialImage(selectedId.value, row.id, { enabled: !row.enabled })
    if (command === 'bind') { binding.value = row; bindId.value = row.material_id; await searchMaterialOptions(); bindDialog.value = true; return }
    await Promise.all([loadImages(), loadMaterials()]); ElMessage.success('操作成功')
  } catch { /* cancellation */ }
}
async function bind() { if (!selectedId.value || !binding.value) return; saving.value = true; try { await api.editMaterialImage(selectedId.value, binding.value.id, { material_id: bindId.value }); bindDialog.value = false; await Promise.all([loadImages(), loadMaterials()]); ElMessage.success('已更新物料关联') } finally { saving.value = false } }
</script>
<template>
  <div class="material-libraries">
    <div class="library-toolbar">
      <el-select v-model="selectedId" placeholder="选择物料库" style="width: 280px"><el-option v-for="lib in libraries" :key="lib.id" :value="lib.id" :label="lib.name" /></el-select>
      <el-tag v-if="selected" :type="selected.enabled ? 'success' : 'info'">{{ selected.enabled ? '已启用' : '已停用' }}</el-tag>
      <div class="spacer" />
      <el-button :icon="Setting" @click="componentsDialog = true">图片组件</el-button>
      <el-button v-if="canEdit" @click="openLibrary()">新建物料库</el-button>
      <el-dropdown v-if="selected && canEdit" @command="(c: string) => c === 'edit' ? openLibrary(selected) : removeLibrary()"><el-button :icon="MoreFilled" aria-label="更多物料库操作" /><template #dropdown><el-dropdown-menu><el-dropdown-item command="edit">修改库设置</el-dropdown-item><el-dropdown-item command="delete" divided>删除物料库</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
    </div>
    <el-empty v-if="!selected" description="尚无物料库，点击「新建物料库」开始管理物料照片" />
    <template v-else>
      <el-alert v-if="!selected.engine_config.embedding_id" type="info" title="尚未选择图像向量组件，请先配置图片组件并在库设置中选择；可先建立物料信息" :closable="false" class="setup-alert" />
      <el-tabs v-model="tab">
        <el-tab-pane label="物料管理" name="materials">
          <div class="section-toolbar"><el-input v-model="query" placeholder="搜索物料编码或名称" clearable style="width: 280px" @input="page = 1" /><div class="spacer" /><el-button v-if="canEdit" type="primary" @click="openMaterial()">新增物料</el-button></div>
          <el-table v-loading="loading" :data="materials">
            <el-table-column prop="code" label="物料编码" min-width="150" show-overflow-tooltip />
            <el-table-column prop="name" label="物料名称" min-width="220" show-overflow-tooltip />
            <el-table-column prop="specification" label="规格" min-width="160" show-overflow-tooltip />
            <el-table-column prop="category" label="分类" width="120" show-overflow-tooltip />
            <el-table-column prop="image_count" label="图片" width="70" />
            <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '已启用' : '已停用' }}</el-tag></template></el-table-column>
            <el-table-column label="操作" width="190"><template #default="{ row }"><el-button link type="primary" @click="showMaterialImages(row.id)">查看图片</el-button><el-button v-if="canEdit" link type="primary" @click="openMaterial(row as api.Material)">编辑</el-button><el-dropdown v-if="canEdit" @command="(c: string) => materialCommand(c, row as api.Material)"><el-button link :icon="MoreFilled" aria-label="更多物料操作" /><template #dropdown><el-dropdown-menu><el-dropdown-item command="toggle">{{ row.enabled ? '停用物料' : '启用物料' }}</el-dropdown-item><el-dropdown-item command="delete" divided>删除物料</el-dropdown-item></el-dropdown-menu></template></el-dropdown></template></el-table-column>
            <template #empty><el-empty description="暂无物料，点击「新增物料」录入料号与规格" /></template>
          </el-table>
          <el-pagination v-model:current-page="page" :total="total" :page-size="30" layout="total, prev, pager, next" />
        </el-tab-pane>
        <el-tab-pane label="图片管理" name="images">
          <div class="section-toolbar"><el-select v-model="imageFilter" style="width: 300px" @change="imagePage = 1"><el-option value="" label="全部图片" /><el-option value="unassigned" label="未关联物料" /><el-option v-if="imageFilter && imageFilter !== 'unassigned'" :value="imageFilter" :label="filterName || '当前物料'" /></el-select><el-button @click="loadImages">刷新状态</el-button><div class="spacer" /><el-button v-if="canEdit" type="primary" @click="openUpload">上传图片</el-button></div>
          <div v-loading="imageLoading" class="image-area"><el-empty v-if="!images.length" description="暂无图片，上传物料照片开始建立图像索引" />
            <div v-else class="image-grid"><article v-for="img in images" :key="img.id" class="image-card">
              <MaterialImageView :library-id="selected.id" :image-id="img.id" :variant="img.has_standard ? 'standard' : 'original'" :version="img.run_id" />
              <div class="image-name" :title="img.filename">{{ img.filename }}</div>
              <div class="status-line"><el-tag :type="stateType(img.status)" size="small">{{ stateLabels[img.status] || img.status }}</el-tag><el-tag v-if="!img.enabled" type="info" size="small">已停用</el-tag><el-tag v-if="!img.material_id" type="info" size="small">未关联物料</el-tag></div>
              <el-tooltip :content="img.message || (img.preprocess ? '已选择图片预加工' : '直接标准化原图')"><div class="image-message">{{ img.message || (img.preprocess ? '图片预加工' : '原图标准化') }}</div></el-tooltip>
              <div class="image-actions"><el-button link type="primary" @click="detailImage = img; detailDialog = true">查看对比</el-button><el-button v-if="canEdit" link type="primary" @click="retry(img)">重新处理</el-button><el-dropdown v-if="canEdit" @command="(c: string) => imageCommand(c, img)"><el-button link :icon="MoreFilled" aria-label="更多图片操作" /><template #dropdown><el-dropdown-menu><el-dropdown-item command="bind">关联物料</el-dropdown-item><el-dropdown-item command="toggle">{{ img.enabled ? '停用图片' : '启用图片' }}</el-dropdown-item><el-dropdown-item command="delete" divided>删除图片</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div>
            </article></div>
          </div>
          <el-pagination v-model:current-page="imagePage" :total="imageTotal" :page-size="12" layout="total, prev, pager, next" />
        </el-tab-pane>
        <el-tab-pane label="检索测试" name="search" lazy><MaterialSearchPanel :key="selected.id" :library-id="selected.id" :enabled="selected.enabled" @material="showMaterialImages" /></el-tab-pane>
      </el-tabs>
    </template>
    <el-dialog v-model="libDialog" :title="editLibId ? '修改物料库' : '新建物料库'" width="640px" append-to-body>
      <el-form label-width="120px"><el-form-item label="物料库名称" required><el-input v-model="libForm.name" maxlength="200" /></el-form-item><el-form-item label="说明"><el-input v-model="libForm.description" type="textarea" :rows="2" maxlength="4000" /></el-form-item>
        <el-form-item label="图像向量组件"><el-select v-model="libForm.config.embedding_id" clearable placeholder="可稍后配置" @clear="libForm.config.embedding_id = null"><el-option v-for="c in embeddings" :key="c.id" :value="c.id" :label="c.name" /></el-select></el-form-item>
        <el-form-item label="图片预加工"><el-switch v-model="libForm.config.preprocess" aria-label="默认图片预加工" /><span class="switch-label">{{ libForm.config.preprocess ? '已开启' : '已关闭' }}</span></el-form-item>
        <el-form-item label="抠图组件"><el-select v-model="libForm.config.remover_id" :disabled="!libForm.config.preprocess" clearable placeholder="选择抠图 HTTP 服务" @clear="libForm.config.remover_id = null"><el-option v-for="c in removers" :key="c.id" :value="c.id" :label="c.name" /></el-select></el-form-item>
        <el-form-item label="开放检索"><el-switch v-model="libForm.enabled" aria-label="物料库开放检索" /><span class="switch-label">{{ libForm.enabled ? '已启用' : '已停用' }}</span></el-form-item>
        <el-tooltip content="设置变更只影响新上传图片；已有图片保留原模型版本，可通过重新处理切换到库配置"><el-text type="info">已有图片保留处理版本</el-text></el-tooltip>
      </el-form><template #footer><el-button @click="libDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="saveLibrary">保存物料库</el-button></template>
    </el-dialog>
    <el-dialog v-model="materialDialog" :title="editMaterialId ? '编辑物料' : '新增物料'" width="640px" append-to-body>
      <el-form label-width="100px"><el-form-item label="物料编码" required><el-input v-model="matForm.code" maxlength="120" /></el-form-item><el-form-item label="物料名称" required><el-input v-model="matForm.name" maxlength="200" /></el-form-item><el-form-item label="规格"><el-input v-model="matForm.specification" maxlength="500" /></el-form-item><el-form-item label="分类"><el-input v-model="matForm.category" maxlength="120" /></el-form-item><el-form-item label="标签"><el-select v-model="matForm.tags" multiple filterable allow-create default-first-option :multiple-limit="20" /></el-form-item><el-form-item label="说明"><el-input v-model="matForm.description" type="textarea" :rows="3" maxlength="10000" /></el-form-item><el-form-item label="开放检索"><el-switch v-model="matForm.enabled" aria-label="物料开放检索" /><span class="switch-label">{{ matForm.enabled ? '已启用' : '已停用' }}</span></el-form-item></el-form>
      <template #footer><el-button @click="materialDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="saveMaterial">保存物料</el-button></template>
    </el-dialog>
    <el-dialog v-model="uploadDialog" title="上传物料图片" width="700px" append-to-body destroy-on-close :close-on-click-modal="false">
      <MaterialImagePicker @change="(f, c) => { uploadFile = f; uploadCrop = c }" />
      <el-form label-width="120px" class="upload-options"><el-form-item label="关联物料"><el-select v-model="uploadMaterialId" filterable remote clearable :remote-method="searchMaterialOptions" placeholder="搜索料号或名称，可留空" @clear="uploadMaterialId = null"><el-option v-for="m in materialOptions" :key="m.id" :value="m.id" :label="`${m.code} · ${m.name}`" /></el-select></el-form-item><el-form-item label="图片预加工"><el-switch v-model="uploadPreprocess" aria-label="本次图片预加工" /><span class="switch-label">{{ uploadPreprocess ? '已开启' : '已关闭' }}</span></el-form-item></el-form>
      <el-alert v-if="uploadPreprocess && !selected?.engine_config.remover_id" title="尚未选择抠图组件，请先配置，或明确关闭本次图片预加工" type="warning" :closable="false" />
      <template #footer><el-button @click="uploadDialog = false">取消</el-button><el-button type="primary" :loading="saving" :disabled="!uploadFile || (uploadPreprocess && !selected?.engine_config.remover_id)" @click="upload">上传并处理</el-button></template>
    </el-dialog>
    <el-dialog v-model="detailDialog" title="图片处理对比" width="1000px" append-to-body>
      <div v-if="detailImage && selected" class="compare-grid"><div><p>原始图片</p><MaterialImageView :library-id="selected.id" :image-id="detailImage.id" variant="original" /></div><div><p>抠图结果</p><MaterialImageView v-if="detailImage.has_foreground" :library-id="selected.id" :image-id="detailImage.id" variant="foreground" :version="detailImage.run_id" /><el-empty v-else :description="detailImage.preprocess ? '尚无抠图产物' : '未启用抠图'" /></div><div><p>向量化标准图</p><MaterialImageView v-if="detailImage.has_standard" :library-id="selected.id" :image-id="detailImage.id" :version="detailImage.run_id" /><el-empty v-else description="尚未完成标准化" /></div></div>
      <el-alert v-if="detailImage?.message" :title="detailImage.message" type="warning" :closable="false" />
    </el-dialog>
    <el-dialog v-model="retryDialog" title="重新处理图片" width="540px" append-to-body>
      <el-form label-width="140px"><el-form-item label="采用当前库组件"><el-switch v-model="useLibraryConfig" aria-label="采用当前库组件" /><span class="switch-label">{{ useLibraryConfig ? '已开启' : '保留原组件' }}</span></el-form-item><el-form-item label="图片预加工"><el-switch v-model="retryPreprocess" aria-label="重新处理时抠图" /><span class="switch-label">{{ retryPreprocess ? '已开启' : '已关闭' }}</span></el-form-item></el-form>
      <el-alert title="重新处理将移除该图片旧索引；完成后恢复检索。若抠图失败，可明确关闭预加工后重试" type="warning" :closable="false" />
      <template #footer><el-button @click="retryDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="runRetry">重新处理</el-button></template>
    </el-dialog>
    <el-dialog v-model="bindDialog" title="关联物料" width="560px" append-to-body><el-select v-model="bindId" filterable remote clearable :remote-method="searchMaterialOptions" placeholder="选择物料，留空取消关联" style="width: 450px" @clear="bindId = null"><el-option v-for="m in materialOptions" :key="m.id" :value="m.id" :label="`${m.code} · ${m.name}`" /></el-select><template #footer><el-button @click="bindDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="bind">保存关联</el-button></template></el-dialog>
    <el-dialog v-model="componentsDialog" title="图片组件配置" width="1000px" append-to-body destroy-on-close><ImageComponentsPanel @changed="loadComponents" /></el-dialog>
  </div>
</template>
<style scoped>
.material-libraries { padding: 20px; background: white; min-height: 100%; box-sizing: border-box; }
.library-toolbar, .section-toolbar { display: flex; gap: 12px; align-items: center; margin-bottom: 20px; }
.spacer { flex: 1; }
.setup-alert { margin-bottom: 16px; }
.el-pagination { margin-top: 20px; justify-content: flex-end; }
.image-area { min-height: 320px; }
.image-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; }
.image-card { min-width: 0; padding: 12px; border: 1px solid var(--el-border-color-light); border-radius: 8px; }
.image-name { margin-top: 12px; font-weight: 500; }
.image-name, .image-message { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status-line { display: flex; align-items: center; gap: 6px; height: 34px; }
.image-message { height: 24px; line-height: 24px; font-size: 12px; color: var(--el-text-color-secondary); }
.image-actions { display: flex; align-items: center; justify-content: space-between; margin-top: 8px; }
.switch-label { margin-left: 8px; min-width: 84px; white-space: nowrap; }
.upload-options { margin-top: 24px; }
.el-form .el-input, .el-form .el-textarea, .el-form .el-select { width: 400px; max-width: 100%; }
.compare-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-bottom: 16px; }
.compare-grid .el-empty { height: 220px; box-sizing: border-box; }
</style>
