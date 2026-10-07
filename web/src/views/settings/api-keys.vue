<script setup lang="ts">
import { computed, onBeforeUnmount, onDeactivated, onMounted, ref } from 'vue'
import { ElAlert, ElButton, ElDialog, ElDropdown, ElDropdownItem, ElDropdownMenu, ElEmpty,
  ElForm, ElFormItem, ElIcon, ElInput, ElMessage, ElMessageBox, ElOption, ElSelect,
  ElSwitch, ElTable, ElTableColumn, ElTag, ElTooltip } from 'element-plus'
import { BookOpen, Copy, Ellipsis, Plus, Search } from '@lucide/vue'
import KgPagination from '@/components/common/KgPagination.vue'
import { createPlatformKey, deletePlatformKey, listPlatformKeys, updatePlatformKey,
  type PlatformKey } from '@/api/platform-keys'
import { openApiGuide } from '@/utils/api-docs'

const rows = ref<PlatformKey[]>([])
const isLoading = ref(false)
const error = ref('')
const query = ref('')
const activeQuery = ref('')
const status = ref('')
const page = ref(1)
const size = ref(20)
const isSaving = ref(false)
const busyId = ref('')
const dialog = ref(false)
const editId = ref('')
const name = ref('')
const secret = ref('')
const secretDialog = ref(false)
const filtered = computed(() => rows.value.filter(row =>
  (!activeQuery.value || `${row.name} ${row.client_id}`.toLowerCase().includes(activeQuery.value.toLowerCase())) &&
  (!status.value || row.enabled === (status.value === 'enabled'))))
const visibleRows = computed(() => filtered.value.slice((page.value - 1) * size.value, page.value * size.value))

async function load() {
  isLoading.value = true
  error.value = ''
  try { rows.value = await listPlatformKeys() }
  catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } }).response?.data?.detail
    error.value = detail || '无法加载 API Key，请检查服务连接后重试'
  } finally { isLoading.value = false }
}
function search() { activeQuery.value = query.value.trim(); page.value = 1 }
function reset() { query.value = ''; status.value = ''; search() }
function openCreate() { editId.value = ''; name.value = ''; dialog.value = true }
function openEdit(row: PlatformKey) { editId.value = row.id; name.value = row.name; dialog.value = true }
async function save() {
  if (!name.value.trim()) return void ElMessage.warning('请输入应用名称')
  isSaving.value = true
  try {
    if (editId.value) {
      await updatePlatformKey(editId.value, { name: name.value.trim() })
      ElMessage.success('应用名称已更新')
    } else {
      const result = await createPlatformKey(name.value.trim())
      secret.value = result.raw_key
      secretDialog.value = true
      ElMessage.success('API Key 已签发')
    }
    dialog.value = false
    await load()
  } catch { /* API 层显示具体错误 */ }
  finally { isSaving.value = false }
}
async function toggle(row: PlatformKey) {
  if (!row.enabled) return changeStatus(row)
  try { await ElMessageBox.confirm(`停用「${row.name}」后，该应用将无法继续调用平台 API。`, '停用 API Key', { type: 'warning' }) }
  catch { return }
  await changeStatus(row)
}
async function changeStatus(row: PlatformKey) {
  busyId.value = row.id
  try {
    const updated = await updatePlatformKey(row.id, { enabled: !row.enabled })
    Object.assign(row, updated)
    ElMessage.success(row.enabled ? 'API Key 已启用' : 'API Key 已停用')
  } catch { /* API 层显示具体错误 */ }
  finally { busyId.value = '' }
}
async function remove(row: PlatformKey) {
  try { await ElMessageBox.confirm(`删除「${row.name}」后，API Key 将永久失效，无法恢复。`, '删除 API Key', { type: 'warning', confirmButtonText: '删除' }) }
  catch { return }
  busyId.value = row.id
  try {
    await deletePlatformKey(row.id)
    ElMessage.success('API Key 已删除')
    await load()
    page.value = Math.max(1, Math.min(page.value, Math.ceil(filtered.value.length / size.value)))
  } catch { /* API 层显示具体错误 */ }
  finally { busyId.value = '' }
}
async function copy() {
  try { await navigator.clipboard.writeText(secret.value); ElMessage.success('API Key 已复制') }
  catch { ElMessage.error('无法复制，请选中密钥后手动复制') }
}
function closeSecret() { secret.value = ''; secretDialog.value = false }
function formatTime(value: string | null) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—' }
onMounted(load)
onDeactivated(closeSecret)
onBeforeUnmount(closeSecret)
</script>

<template>
  <section class="api-keys-page">
    <el-form inline class="filter-bar" @submit.prevent="search">
      <el-form-item label="应用名称"><el-input v-model="query" clearable placeholder="搜索应用名称或应用标识" @keyup.enter="search" /></el-form-item>
      <el-form-item label="状态"><el-select v-model="status" placeholder="全部状态" clearable @change="search"><el-option label="已启用" value="enabled" /><el-option label="已停用" value="disabled" /></el-select></el-form-item>
      <div class="filter-actions"><el-button type="primary" plain :icon="Search" @click="search">查询</el-button><el-button @click="reset">重置</el-button></div>
    </el-form>
    <div class="toolbar">
      <el-button type="primary" :icon="Plus" :disabled="isLoading || !!error" @click="openCreate">签发 Key</el-button>
      <el-button :icon="BookOpen" @click="openApiGuide('authentication')">查看调用说明</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon><el-button link type="primary" @click="load">重新加载</el-button></el-alert>
    <el-table v-loading="isLoading" :data="visibleRows" border class="keys-table">
      <el-table-column prop="name" label="应用名称" min-width="260" show-overflow-tooltip />
      <el-table-column prop="client_id" label="应用标识" width="260" show-overflow-tooltip />
      <el-table-column label="权限" width="132"><template #default><el-tag type="info" effect="plain">知识库检索</el-tag></template></el-table-column>
      <el-table-column label="状态" width="132"><template #default="{ row }"><div class="status-cell"><el-switch :model-value="row.enabled" :loading="busyId === row.id" :disabled="!!busyId" :aria-label="`${row.name}启用状态`" @change="toggle(row as PlatformKey)" /><span>{{ row.enabled ? '已启用' : '已停用' }}</span></div></template></el-table-column>
      <el-table-column label="签发时间" width="180"><template #default="{ row }">{{ formatTime(row.created_at) }}</template></el-table-column>
      <el-table-column label="操作" width="120" fixed="right"><template #default="{ row }">
        <el-button link size="small" type="primary" :disabled="!!busyId" @click="openEdit(row as PlatformKey)">重命名</el-button>
        <el-dropdown trigger="click" placement="bottom-end" @command="remove(row as PlatformKey)"><el-button link size="small" :disabled="!!busyId" aria-label="更多操作"><el-tooltip content="更多操作"><el-icon><Ellipsis /></el-icon></el-tooltip></el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="delete" class="danger-action">删除 Key</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
      </template></el-table-column>
      <template #empty><el-empty :description="error ? '服务暂不可用，请重新加载' : (rows.length ? '没有符合筛选条件的 API Key' : '尚无 API Key，点击「签发 Key」添加接入应用')" /></template>
    </el-table>
    <KgPagination :total="filtered.length" :page="page" :size="size" layout="total, sizes, prev, pager, next" @update:page="page = $event" @update:size="size = $event; page = 1" />
    <el-dialog v-model="dialog" :title="editId ? '重命名应用' : '签发 API Key'" width="480px" :close-on-click-modal="false" :show-close="!isSaving" :close-on-press-escape="!isSaving">
      <el-form label-width="88px" @submit.prevent="save"><el-form-item label="应用名称" required><el-input v-model="name" maxlength="100" show-word-limit placeholder="例如：售后服务系统" :disabled="isSaving" /></el-form-item><el-form-item label="访问权限"><el-tag effect="plain">知识库检索</el-tag><el-tooltip content="可检索所有已启用的非素材知识库；同一 Key 在平台内统一使用"><el-button link class="scope-help">查看范围</el-button></el-tooltip></el-form-item></el-form>
      <template #footer><el-button :disabled="isSaving" @click="dialog = false">取消</el-button><el-button type="primary" :loading="isSaving" @click="save">{{ editId ? '保存' : '签发 Key' }}</el-button></template>
    </el-dialog>
    <el-dialog v-model="secretDialog" title="保存 API Key" width="600px" :close-on-click-modal="false" :close-on-press-escape="false" :show-close="false">
      <el-alert title="完整密钥仅显示这一次，请复制并妥善保存。关闭后无法再次查看。" type="warning" :closable="false" show-icon />
      <el-input :model-value="secret" type="textarea" :rows="4" readonly aria-label="新签发的 API Key" class="secret-input" />
      <template #footer><el-button @click="closeSecret">已保存，关闭</el-button><el-button type="primary" :icon="Copy" @click="copy">复制 Key</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped lang="scss">
.api-keys-page { padding: 24px; background: var(--el-bg-color); min-height: 100%; box-sizing: border-box; }
.filter-bar { display: flex; flex-wrap: wrap; gap: 16px; align-items: center; }
.filter-bar .el-form-item { margin: 0; }
.filter-bar .el-input { width: 256px; }
.filter-bar .el-select { width: 144px; }
.filter-actions { margin-left: auto; display: flex; }
.toolbar { display: flex; justify-content: space-between; margin: 24px 0 16px; }
.keys-table { margin-top: 16px; min-height: 320px; }
.status-cell { display: flex; gap: 8px; align-items: center; white-space: nowrap; }
.scope-help { margin-left: 8px; }
.secret-input { margin-top: 16px; }
.danger-action { color: var(--el-color-danger); }
</style>
