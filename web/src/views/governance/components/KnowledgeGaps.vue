<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { getGaps, exportGaps, getOwnerTemplate, importGapOwners, saveGapOwner, saveDingtalkOwner,
  notifyGapOwner, refreshDingtalkFolders, getDingtalkRefreshStatus, type DingtalkRefreshStatus,
  type KnowledgeGap, type GapFilters } from '@/api/governance'
import { listKnowledgeSources } from '@/api/knowledge-center'

const user = useUserStore()
const canEdit = computed(() => ['admin', 'super_admin', 'editor'].includes(user.userInfo?.role || ''))
const filters = reactive<GapFilters>({ path: '', document_state: 'all', document_count: '', folder_count: '' })
const rows = ref<KnowledgeGap[]>([])
// 知识库下拉选项：来自知识源管理注册表，只取已启用的钉钉知识库（value 为知识库 external_id）
const kbs = ref<{ id: string; name: string }[]>([])
const owners = ref<string[]>([])
const page = ref(1)
const size = ref(20)
const total = ref(0)
const loading = ref(false)
const exporting = ref(false)
const importing = ref(false)
const input = ref<HTMLInputElement>()
const editRow = ref<KnowledgeGap>()
const ownerValue = ref('')
const dialog = ref(false)
const saving = ref(false)
let sequence = 0

// 加载知识库过滤选项：知识源管理中已启用的钉钉知识库
async function loadKbOptions() {
  try {
    const sources = await listKnowledgeSources({ source_type: 'dingtalk_workspace', enabled_only: true })
    kbs.value = sources.map((s) => ({ id: s.external_id, name: s.name }))
  } catch { /* API interceptor displays errors. */ }
}

async function load(reset = false) {
  if (reset) page.value = 1
  const current = ++sequence
  loading.value = true
  try {
    const data = await getGaps({ ...filters, page: page.value, size: size.value })
    if (current !== sequence) return
    if (data.error) ElMessage.error(data.error)
    rows.value = data.items
    total.value = data.total
    owners.value = data.owners
  } catch { /* API interceptor displays errors. */ }
  finally { if (current === sequence) loading.value = false }
}
function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 60000)
}

// 漏斗弹层内处于生效状态的次要筛选项数量（用于漏斗角标提示）
const advancedCount = computed(() =>
  (filters.document_state !== 'all' ? 1 : 0) +
  (filters.document_count ? 1 : 0) +
  (filters.folder_count ? 1 : 0))

// 「更多操作」下拉：导出筛选结果 / 导入 Owner / 下载导入模板
function onMoreCommand(cmd: string) {
  if (cmd === 'export') exportFile()
  else if (cmd === 'template') exportFile(true)
  else if (cmd === 'import') input.value?.click()
}
async function exportFile(template = false) {
  exporting.value = true
  try {
    // 模板：选中钉钉知识库时导出该库文件夹行，编辑后可直接导回
    const params = template && isDingtalkSelected.value ? { kb_id: filters.kb_id } : undefined
    download(await (template ? getOwnerTemplate(params) : exportGaps({ ...filters })), template ? '知识Owner导入模板.csv' : '知识缺口.csv')
  } catch { /* API interceptor displays errors. */ }
  finally { exporting.value = false }
}
async function importFile(event: Event) {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return
  importing.value = true
  try {
    const result = await importGapOwners(file)
    ElMessage.success(`已更新 ${result.updated} 个目录的知识Owner`)
    await load()
  } catch { /* API interceptor displays row validation errors. */ }
  finally { importing.value = false; target.value = '' }
}
function edit(row: KnowledgeGap) {
  editRow.value = row
  ownerValue.value = row.owner
  dialog.value = true
}
async function save() {
  if (!editRow.value) return
  saving.value = true
  try {
    // 钉钉文件夹行（source=dingtalk）写快照表 Owner；本地目录行走原接口
    if (editRow.value.source === 'dingtalk') {
      await saveDingtalkOwner(editRow.value.kb_id, editRow.value.directory_id, ownerValue.value)
    } else {
      await saveGapOwner(editRow.value.directory_id, ownerValue.value)
    }
    ElMessage.success('知识Owner已更新')
    dialog.value = false
    await load()
  } catch { /* API interceptor displays errors. */ }
  finally { saving.value = false }
}

// ===== 通知：通过钉钉机器人给知识Owner发私聊提醒（仅无文档且已维护 Owner 的钉钉行）=====
const notifyRow = ref<KnowledgeGap>()
const notifyDialog = ref(false)
const notifying = ref(false)

function canNotify(row: KnowledgeGap) {
  return row.source === 'dingtalk' && row.document_count === 0 && !!row.owner
}
function notifyTip(row: KnowledgeGap) {
  if (!row.owner) return '未维护知识Owner，无法通知'
  if (row.document_count > 0) return '该目录已有文档，无需补充'
  return '通过钉钉机器人发单聊提醒'
}
function openNotify(row: KnowledgeGap) {
  notifyRow.value = row
  notifyDialog.value = true
}
async function sendNotify() {
  if (!notifyRow.value) return
  notifying.value = true
  try {
    const res = await notifyGapOwner(notifyRow.value.kb_id, notifyRow.value.directory_id)
    ElMessage.success(res.message || '通知已发送')
    notifyDialog.value = false
  } catch { /* API interceptor displays errors. */ }
  finally { notifying.value = false }
}
onMounted(() => { loadKbOptions(); load() })

// ===== 钉钉知识库文件夹快照：刷新按钮触发后台遍历，轮询进度 =====
// 遍历任务跑在后端，前端只镜像状态：running 以后端 status 为准，
// 切换知识库不中断任务，切回仍在遍历的库时恢复「正在遍历」展示与按钮禁用
const dtStatus = ref<DingtalkRefreshStatus | null>(null)
// 点击「刷新数据」到后端状态可读之间的过渡态，防止按钮短暂可点
const dtStarting = ref(false)
const dtRefreshing = computed(() => dtStarting.value || !!dtStatus.value?.running)
let pollTimer: number | undefined
let statusSeq = 0
const isDingtalkSelected = computed(() => !!filters.kb_id)

function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = undefined }
}

async function fetchDtStatus() {
  const kb = filters.kb_id
  if (!kb) { dtStatus.value = null; return }
  const seq = ++statusSeq
  try {
    const st = await getDingtalkRefreshStatus(kb)
    if (seq !== statusSeq) return // 已切换知识库，丢弃过期结果
    dtStatus.value = st
  } catch { /* 忽略状态查询失败 */ }
}

function pollStatus() {
  stopPolling()
  pollTimer = window.setTimeout(async () => {
    const kb = filters.kb_id
    const wasRunning = !!dtStatus.value?.running
    await fetchDtStatus()
    if (filters.kb_id !== kb) return // 轮询期间切库，由 onKbChange 接管
    const st = dtStatus.value
    if (st?.running) { pollStatus(); return }
    stopPolling()
    dtStarting.value = false
    if (!wasRunning) return // 切入时已是 idle 状态，不做完成提示
    if (st?.error) { ElMessage.error(`刷新失败：${st.error}`); return }
    if (st?.truncated) {
      ElMessage.warning(`刷新完成，共 ${st?.folder_count ?? 0} 个文件夹（已达遍历上限，数据可能不完整）`)
    } else {
      ElMessage.success(`刷新完成，共 ${st?.folder_count ?? 0} 个文件夹`)
    }
    load()
  }, 2000)
}

async function refreshDingtalk() {
  const kb = filters.kb_id
  if (!kb || dtRefreshing.value) return
  try { await refreshDingtalkFolders(kb) } catch { return }
  if (filters.kb_id !== kb) return // 点击后立即切库，状态由 onKbChange 接管
  dtStarting.value = true
  await fetchDtStatus()
  pollStatus()
}

// 切换知识库：仅切换轮询对象，不中断后端任务；新库仍在遍历则立即恢复「正在遍历」
async function onKbChange() {
  stopPolling()
  dtStarting.value = false
  const kb = filters.kb_id
  await fetchDtStatus()
  if (filters.kb_id !== kb) return
  if (dtStatus.value?.running) pollStatus()
}

onUnmounted(stopPolling)
</script>

<template>
  <el-card shadow="never">
    <div class="filters">
      <!-- 筛选条件变更不自动查询，统一由「查询」按钮触发（钉钉知识库遍历耗时较长） -->
      <el-select v-model="filters.kb_id" placeholder="全部知识库" aria-label="知识库过滤" clearable filterable @change="onKbChange">
        <el-option v-for="kb in kbs" :key="kb.id" :label="kb.name" :value="kb.id" />
      </el-select>
      <el-select v-model="filters.owner" placeholder="全部 Owner" aria-label="Owner过滤" clearable filterable>
        <el-option v-for="owner in owners" :key="owner" :label="owner" :value="owner" />
      </el-select>
      <el-input v-model="filters.path" class="path-input" placeholder="目录路径模糊搜索" aria-label="目录路径过滤" clearable @keyup.enter="load(true)">
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <!-- 次要筛选折叠进漏斗弹层：只外显 3 个高频搜索项，点漏斗从其下方展开其余筛选项 -->
      <el-popover placement="bottom-start" :width="232" trigger="click" popper-class="gap-adv-popover">
        <template #reference>
          <el-badge :value="advancedCount" :hidden="!advancedCount" class="funnel-badge">
            <el-button aria-label="更多筛选"><el-icon><Filter /></el-icon></el-button>
          </el-badge>
        </template>
        <div class="adv-filters">
          <el-select v-model="filters.document_state" aria-label="有无文档过滤">
            <el-option label="全部文档状态" value="all" />
            <el-option label="无文档" value="empty" />
            <el-option label="有文档" value="has" />
          </el-select>
          <el-select v-model="filters.document_count" aria-label="文档数量过滤">
            <el-option label="全部文档数量" value="" />
            <el-option label="0 个" value="0" />
            <el-option label="1-9 个" value="1-9" />
            <el-option label="10-99 个" value="10-99" />
            <el-option label="100 个以上" value="100+" />
          </el-select>
          <el-select v-model="filters.folder_count" aria-label="文件夹数量过滤">
            <el-option label="全部文件夹数量" value="" />
            <el-option label="0 个" value="0" />
            <el-option label="1-9 个" value="1-9" />
            <el-option label="10-99 个" value="10-99" />
            <el-option label="100 个以上" value="100+" />
          </el-select>
        </div>
      </el-popover>
      <el-button type="primary" :loading="loading" @click="load(true)">查询</el-button>
      <!-- 常驻显示，未选钉钉知识库时禁用，避免用户找不到入口；? 图标绝对定位在按钮右上角 -->
      <div class="refresh-btn-wrap">
        <el-button type="warning" :disabled="!isDingtalkSelected || dtRefreshing" :loading="dtRefreshing" @click="refreshDingtalk()">刷新数据</el-button>
        <el-tooltip placement="top" popper-class="gap-tip">
          <template #content>
            <p class="gap-tip-text">按目录查看知识覆盖情况。选择钉钉知识库后点「查询」读取其文件夹快照，文档数量为文件夹直属文档数（含在线文档与本地上传文件，不含子文件夹）；数量为 0 即知识缺口。快照不会自动更新，点击「刷新数据」后台重新遍历（大知识库需数分钟）。</p>
          </template>
          <el-icon class="q refresh-q"><QuestionFilled /></el-icon>
        </el-tooltip>
      </div>
      <span v-if="isDingtalkSelected && dtStatus" class="dt-status">
        <template v-if="dtRefreshing">正在遍历，已获取 {{ dtStatus.done }} 个文件夹…</template>
        <template v-else-if="dtStatus.folder_count">快照：{{ dtStatus.folder_count }} 个文件夹{{ dtStatus.fetched_at ? `（${dtStatus.fetched_at.replace('T', ' ')}）` : '' }}{{ dtStatus.truncated ? '，已达遍历上限，数据可能不完整' : '' }}</template>
        <template v-else>暂无快照，请点击「刷新数据」</template>
      </span>
      <!-- 导出/导入收进「更多操作」下拉，减少行内按钮数量；导入项仅编辑角色可见 -->
      <el-dropdown @command="onMoreCommand">
        <el-button :loading="exporting || importing">
          更多操作<el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="export">导出筛选结果</el-dropdown-item>
            <template v-if="canEdit">
              <el-dropdown-item command="import">导入 Owner</el-dropdown-item>
              <el-dropdown-item command="template">下载导入模板</el-dropdown-item>
            </template>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <input ref="input" type="file" accept=".csv,.xlsx" hidden @change="importFile" />
    </div>
    <el-table :data="rows" v-loading="loading" style="width: 100%">
      <el-table-column label="知识库" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <a v-if="row.kb_url" :href="row.kb_url" target="_blank" rel="noopener" class="cell-link">{{ row.kb_name }}</a>
          <span v-else>{{ row.kb_name }}</span>
        </template>
      </el-table-column>
      <el-table-column label="目录路径" min-width="280" show-overflow-tooltip>
        <template #default="{ row }">
          <a v-if="row.dingtalk_url" :href="row.dingtalk_url" target="_blank" rel="noopener" class="cell-link">{{ row.directory_path }}</a>
          <span v-else>{{ row.directory_path }}</span>
        </template>
      </el-table-column>
      <el-table-column label="知识Owner" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">{{ row.owner || '未维护' }}</template>
      </el-table-column>
      <el-table-column prop="document_count" label="文档数量" width="100" align="right" />
      <el-table-column prop="folder_count" label="文件夹数量" width="110" align="right" />
      <el-table-column label="文档状态" width="100">
        <template #default="{ row }"><el-tag :type="row.document_count ? 'success' : 'warning'">{{ row.document_count ? '有文档' : '无文档' }}</el-tag></template>
      </el-table-column>
      <el-table-column v-if="canEdit" label="操作" width="170">
        <template #default="{ row }">
          <el-button link type="primary" @click="edit(row as KnowledgeGap)">维护 Owner</el-button>
          <!-- 仅钉钉行提供通知；无文档且有 Owner 时可点，其余禁用并以 tooltip 说明 -->
          <el-tooltip v-if="(row as KnowledgeGap).source === 'dingtalk'" :disabled="canNotify(row as KnowledgeGap)" :content="notifyTip(row as KnowledgeGap)">
            <el-button link type="warning" :disabled="!canNotify(row as KnowledgeGap)" @click="openNotify(row as KnowledgeGap)">通知</el-button>
          </el-tooltip>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty :description="isDingtalkSelected && !dtStatus?.folder_count ? '该知识库还没有快照数据，请点击「刷新数据」' : '没有符合筛选条件的目录'" />
      </template>
    </el-table>
    <el-pagination v-model:current-page="page" v-model:page-size="size" :total="total" :page-sizes="[20, 50, 100]" layout="total, sizes, prev, pager, next" @current-change="load()" @size-change="load(true)" />
  </el-card>
  <el-dialog v-model="dialog" title="维护知识Owner" width="min(520px, 90vw)" :close-on-click-modal="!saving">
    <p class="owner-path">{{ editRow?.kb_name }} / {{ editRow?.directory_path }}</p>
    <el-form label-position="top" @submit.prevent="save">
      <el-form-item label="知识Owner"><el-input v-model="ownerValue" maxlength="200" show-word-limit placeholder="输入 Owner 姓名或团队；留空清除" /></el-form-item>
    </el-form>
    <template #footer><el-button :disabled="saving" @click="dialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存</el-button></template>
  </el-dialog>
  <!-- 通知确认弹窗：预览将发送的钉钉私聊消息正文 -->
  <el-dialog v-model="notifyDialog" title="发送知识Owner提醒" width="min(520px, 90vw)" :close-on-click-modal="!notifying">
    <p class="owner-path">{{ notifyRow?.kb_name }} / {{ notifyRow?.directory_path }}</p>
    <p class="notify-text">你即将通过钉钉发送私聊通知给知识Owner {{ notifyRow?.owner }}：</p>
    <p class="notify-quote">【{{ notifyRow?.kb_name }} / {{ notifyRow?.directory_path }}】该目录下的知识为空，请尽快补充，谢谢！</p>
    <template #footer><el-button :disabled="notifying" @click="notifyDialog = false">取消</el-button><el-button type="warning" :loading="notifying" @click="sendNotify">发送</el-button></template>
  </el-dialog>
</template>

<style scoped>
.filters { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }
.filters .el-select { width: 200px; }
.filters .path-input { width: 200px; }
/* 漏斗弹层 teleport 到 body：需全局（:global）约束内部筛选项纵向排列、占满弹层宽度 */
:global(.gap-adv-popover .adv-filters) { display: flex; flex-direction: column; gap: 10px; }
:global(.gap-adv-popover .el-select) { width: 100%; }
.cell-link { color: var(--el-color-primary); text-decoration: none; }
.cell-link:hover { text-decoration: underline; }
.filters .q { color: #9CA3AF; font-size: 14px; cursor: help; }
.refresh-btn-wrap { position: relative; display: inline-flex; }
.refresh-q { position: absolute; top: -6px; right: -6px; z-index: 1; }
/* 工具提示弹层 teleport 到 body：需全局（:global）约束宽度为半屏并居中，防止说明过长撑满全宽 */
:global(.gap-tip) { width: min(50vw, 640px); }
:global(.gap-tip .gap-tip-text) { margin: 0; line-height: 1.6; }
.dt-status { color: #909399; font-size: 12.5px; }
.el-pagination { margin-top: 20px; justify-content: flex-end; }
.owner-path { overflow-wrap: anywhere; line-height: 1.6; margin-bottom: 20px; }
.notify-text { line-height: 1.6; margin: 0 0 12px; }
.notify-quote { overflow-wrap: anywhere; line-height: 1.7; margin: 0; padding: 12px 14px; background: #fdf6ec; border-radius: 4px; color: #b88230; }
</style>
