<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { MoreFilled } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import { listImageComponents, createImageComponent, deleteImageComponent, updateImageCredential, testImageComponent, testImageConnection, type ImageComponent, type ImageComponentInput } from '@/api/material-library'
import MaterialImagePicker from './MaterialImagePicker.vue'
const emit = defineEmits<{ changed: [] }>()
const canEdit = computed(() => ['super_admin', 'admin'].includes(useUserStore().userInfo?.role || ''))
const rows = ref<ImageComponent[]>([]), loading = ref(false), saving = ref(false), dialog = ref(false)
const defaults = (): ImageComponentInput => ({ name: '', kind: 'embedding', provider: 'dashscope', endpoint: 'https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding', model: 'multimodal-embedding-v1', dimensions: 1024, api_key: '' })
const form = reactive(defaults())
const testRow = ref<ImageComponent | null>(null), testDialog = ref(false), testFile = ref<File | null>(null), testing = ref(false)
const testCrop = ref<number[] | null>(null), testError = ref('')
const report = ref<{ ok: boolean; dimensions: number | null; elapsed_ms: number; preview: string } | null>(null)
const detailRow = ref<ImageComponent | null>(null), detailDialog = ref(false), connecting = ref(false)
const connection = ref<{ ok: boolean; message: string } | null>(null)
function showConfig(row: ImageComponent) { detailRow.value = row; connection.value = null; detailDialog.value = true }
function failureMessage(error: unknown) {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof detail === 'string' ? detail : '调用失败，请检查服务地址、密钥及网络后重试'
}
async function connect() {
  if (!detailRow.value) return
  connecting.value = true; connection.value = null
  try {
    const result = await testImageConnection(detailRow.value.id)
    connection.value = { ok: true, message: `连通成功 · ${result.elapsed_ms}ms${result.dimensions ? ' · ' + result.dimensions + ' 维' : ''}` }
  } catch (error: unknown) {
    connection.value = { ok: false, message: failureMessage(error) }
  } finally { connecting.value = false }
}
async function load() { loading.value = true; try { rows.value = await listImageComponents() } finally { loading.value = false } }
onMounted(load)
function add() { Object.assign(form, defaults()); dialog.value = true }
function kindChange() {
  if (form.kind === 'remover') Object.assign(form, { provider: 'http', endpoint: '', model: 'BiRefNet', dimensions: 1024 })
  else Object.assign(form, defaults(), { name: form.name })
}
async function save() {
  if (!form.name.trim() || !form.endpoint.trim()) { ElMessage.warning('填写组件名称和接口地址'); return }
  saving.value = true
  try { await createImageComponent(form); form.api_key = ''; dialog.value = false; await load(); emit('changed'); ElMessage.success('已保存组件版本') }
  finally { saving.value = false }
}
function startTest(row: ImageComponent) { testRow.value = row; report.value = null; testError.value = ''; testCrop.value = null; testFile.value = null; testDialog.value = true }
async function runTest() {
  if (!testFile.value || !testRow.value) return
  testing.value = true; report.value = null; testError.value = ''
  try { report.value = await testImageComponent(testRow.value.id, testFile.value, testCrop.value) } catch (error: unknown) { testError.value = failureMessage(error) } finally { testing.value = false }
}
async function command(value: string, row: ImageComponent) {
  try {
    if (value === 'key') {
      const result = await ElMessageBox.prompt('填写新的 API Key，已有密钥不会回显', '更新密钥', { inputType: 'password', inputPlaceholder: 'API Key' })
      await updateImageCredential(row.id, result.value); ElMessage.success('已更新密钥')
    } else {
      await ElMessageBox.confirm(`删除组件「${row.name}」？已被物料库或图片引用的组件不能删除`, '删除组件', { type: 'warning' })
      await deleteImageComponent(row.id); ElMessage.success('已删除组件')
    }
    await load(); if (detailRow.value) detailRow.value = rows.value.find(r => r.id === detailRow.value?.id) || null; connection.value = null; emit('changed')
  } catch { /* API errors are reported by request; cancellation is silent. */ }
}
</script>
<template>
  <div class="components-panel" v-loading="loading">
    <div class="toolbar"><span>图片处理组件</span><el-button v-if="canEdit" type="primary" @click="add">新增组件</el-button></div>
    <el-table :data="rows">
      <el-table-column prop="name" label="组件名称" min-width="190" show-overflow-tooltip />
      <el-table-column label="用途" width="100"><template #default="{ row }">{{ row.kind === 'embedding' ? '图像向量' : '图片抠图' }}</template></el-table-column>
      <el-table-column prop="model" label="模型" min-width="180" show-overflow-tooltip />
      <el-table-column label="服务协议" width="120"><template #default="{ row }">{{ row.provider === 'dashscope' ? '阿里云百炼' : '自定义 HTTP' }}</template></el-table-column>
      <el-table-column label="操作" width="210"><template #default="{ row }">
        <el-button link type="primary" @click="showConfig(row as ImageComponent)">查看配置</el-button>
        <el-button link type="primary" :disabled="!canEdit" @click="startTest(row as ImageComponent)">测试图片</el-button>
        <el-dropdown v-if="canEdit" @command="(c: string) => command(c, row as ImageComponent)"><el-button link :icon="MoreFilled" aria-label="更多组件操作" /><template #dropdown><el-dropdown-menu><el-dropdown-item command="key">更新密钥</el-dropdown-item><el-dropdown-item command="delete" divided>删除组件</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
      </template></el-table-column>
      <template #empty><el-empty description="尚无图片组件，请配置图像向量服务及抠图服务" /></template>
    </el-table>
    <el-dialog v-model="detailDialog" title="图片组件配置" width="650px" append-to-body :close-on-click-modal="false" :before-close="(done: () => void) => { if (!connecting) done() }">
      <el-descriptions class="component-details" v-if="detailRow" :column="1" border>
        <el-descriptions-item label="组件名称">{{ detailRow.name }}</el-descriptions-item>
        <el-descriptions-item label="用途">{{ detailRow.kind === 'embedding' ? '图像向量' : '图片抠图' }}</el-descriptions-item>
        <el-descriptions-item label="服务协议">{{ detailRow.provider === 'dashscope' ? '阿里云百炼' : '自定义 HTTP' }}</el-descriptions-item>
        <el-descriptions-item label="接口地址"><span style="overflow-wrap: anywhere">{{ detailRow.endpoint }}</span></el-descriptions-item>
        <el-descriptions-item label="模型名称">{{ detailRow.model }}</el-descriptions-item>
        <el-descriptions-item label="向量维度">{{ detailRow.kind === 'embedding' ? detailRow.dimensions : '不适用' }}</el-descriptions-item>
        <el-descriptions-item label="API Key"><el-tag :type="detailRow.has_api_key ? 'success' : 'info'">{{ detailRow.has_api_key ? '已配置（密钥不回显）' : '未配置' }}</el-tag><el-button v-if="canEdit" link type="primary" :disabled="connecting" style="margin-left: 12px" @click="command('key', detailRow)">更新密钥</el-button></el-descriptions-item>
      </el-descriptions>
      <div class="connection-report"><el-alert v-if="connection" :type="connection.ok ? 'success' : 'error'" :title="connection.message" :closable="false" /></div>
      <template #footer><el-button :disabled="connecting" @click="detailDialog = false">关闭</el-button><el-tooltip content="使用已保存的配置和内置图片实际调用模型，会产生少量模型调用费用"><el-button v-if="canEdit" type="primary" :loading="connecting" @click="connect">测试连通性</el-button></el-tooltip></template>
    </el-dialog>
    <el-dialog v-model="dialog" title="新增图片组件" width="640px" append-to-body :close-on-click-modal="false" @closed="form.api_key = ''">
      <el-form label-width="110px">
        <el-form-item label="组件名称"><el-input v-model="form.name" maxlength="120" placeholder="例如：百炼图片向量 v1" /></el-form-item>
        <el-form-item label="组件用途"><el-radio-group v-model="form.kind" @change="kindChange"><el-radio-button value="embedding">图像向量</el-radio-button><el-radio-button value="remover">图片抠图</el-radio-button></el-radio-group></el-form-item>
        <el-form-item label="服务协议"><el-select v-model="form.provider" :disabled="form.kind === 'remover'"><el-option label="阿里云百炼" value="dashscope" :disabled="form.kind === 'remover'" /><el-option label="自定义 HTTP" value="http" /></el-select></el-form-item>
        <el-form-item label="接口地址"><el-input v-model="form.endpoint" placeholder="填写完整 HTTP 接口地址" /></el-form-item>
        <el-form-item label="模型名称"><el-input v-model="form.model" /></el-form-item>
        <el-form-item label="向量维度"><el-input-number v-model="form.dimensions" :min="64" :max="4096" :disabled="form.kind === 'remover'" /></el-form-item>
        <el-form-item label="API Key"><el-input v-model="form.api_key" type="password" show-password autocomplete="new-password" placeholder="内网免鉴权服务可留空" /></el-form-item>
        <el-alert type="info" :closable="false" :title="form.kind === 'remover' ? '抠图协议：POST multipart 文件字段 file，返回同尺寸透明 PNG；也支持 JSON 的 image_base64 字段' : '模型、接口和维度作为不可变组件版本保存；更换模型请新增组件，避免混用向量'" />
      </el-form>
      <template #footer><el-button @click="dialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存组件</el-button></template>
    </el-dialog>
    <el-dialog v-model="testDialog" :title="`测试组件 · ${testRow?.name || ''}`" width="650px" append-to-body destroy-on-close>
      <MaterialImagePicker @change="(file, crop) => { testFile = file; testCrop = crop; report = null; testError = '' }" />
      <el-alert v-if="testError" class="test-result" type="error" :closable="false" :title="testError" />
      <div v-if="report" class="test-result"><el-alert type="success" :closable="false" :title="`调用成功 · ${report.elapsed_ms}ms${report.dimensions ? ' · '+report.dimensions+' 维' : ''}`" /><el-image :src="report.preview" fit="contain" style="height: 180px; width: 100%; margin-top: 12px" /></div>
      <template #footer><el-button @click="testDialog = false">关闭</el-button><el-button type="primary" :disabled="!testFile" :loading="testing" @click="runTest">测试图片</el-button></template>
    </el-dialog>
  </div>
</template>
<style scoped>
.component-details :deep(.el-descriptions__label) { width: 100px; white-space: nowrap; }
.connection-report { min-height: 48px; margin-top: 16px; }
.components-panel { padding: 16px 0; }
.toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.el-input { width: 400px; max-width: 100%; }
.el-select { width: 240px; }
.test-result { margin-top: 16px; }
</style>
