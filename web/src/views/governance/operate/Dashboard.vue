<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick, computed } from 'vue'
import * as echarts from 'echarts'
import html2canvas from 'html2canvas'
import { ElMessage } from 'element-plus'
import { Download } from '@lucide/vue'
import {
  getOverview,
  getKnowledgeDistribution,
  getHotDocuments,
  refreshDingtalk,
  getKnowledgeGapDepartments,
  type OverviewResponse,
  type DistributionItem,
  type HotDocsResponse,
  type KnowledgeGapDepartment,
  type KnowledgeGapDepartmentsResponse,
} from '@/api/operate'

// ============ 数据 ============
const loading = ref(false)
const overview = ref<OverviewResponse | null>(null)
const distData = ref<DistributionItem[]>([])
const distError = ref('')
const distLoading = ref(false)
const onlyTop10 = ref(true)
const chartRef = ref<HTMLDivElement>()
const gapChartRef = ref<HTMLDivElement>()
const gapDepartments = ref<KnowledgeGapDepartmentsResponse | null>(null)
const gapLoading = ref(false)
const gapError = ref('')
let chartInstance: echarts.ECharts | null = null
let gapChartInstance: echarts.ECharts | null = null
let pollTimer: ReturnType<typeof setTimeout> | null = null
const onWindowResize = () => {
  chartInstance?.resize()
  gapChartInstance?.resize()
}

const COLORS = ['#3B82F6', '#10B981', '#6366F1', '#F59E0B', '#EF4444', '#8B5CF6', '#EC4899', '#14B8A6', '#F97316', '#06B6D4']

// 存储总量：有权限错误 / 回退标记
const storageError = computed(() => overview.value?.storage?.error || '')
const storageFallback = computed(() => !!overview.value?.storage?.fallback)
// 知识数量是否还在后台统计
const countLoading = computed(() => overview.value?.knowledge_count?.loading && overview.value?.knowledge_count?.value == null)
// 存储权限提示（Storage.Org.Read 未开通）
const storagePermMissing = computed(() => /Storage\.Org\.Read|企业存储企业读权限/.test(storageError.value))
// 钉钉未配置（凭证缺失）
const notConfigured = computed(() => overview.value?.dingtalk_configured === false)

// ============ 加载数据 ============
async function loadAll() {
  loading.value = true
  try {
    // 三个请求互不依赖，一起并发（原先热门知识串在前两个之后，白等一个来回）
    const [ov, dist] = await Promise.all([getOverview(), getKnowledgeDistribution(), loadHot(), loadGapDepartments()])
    overview.value = ov
    distData.value = dist.items || []
    distLoading.value = !!dist.loading
    // 知识数量/分布的错误与存储权限错误分开：存储权限错误只在存储卡片上提示
    distError.value = dist.error || ''
    await nextTick()
    renderChart()
    renderGapChart()
    // 统计任务进行中（或某指标仍在加载）时，20 秒后轮询一次
    const stillLoading = !!ov.job?.running || dist.loading || hotData.value.loading
    if (stillLoading) {
      if (pollTimer) clearTimeout(pollTimer)
      pollTimer = setTimeout(loadAll, 20000)
    }
  } catch (e: any) {
    ElMessage.error(e?.message || '加载运营数据失败')
  } finally {
    loading.value = false
  }
}

async function loadGapDepartments() {
  gapLoading.value = true
  gapError.value = ''
  try {
    gapDepartments.value = await getKnowledgeGapDepartments()
  } catch (e: any) {
    gapError.value = e?.message || '目录覆盖数据加载失败'
  } finally {
    gapLoading.value = false
  }
}

async function doRefresh() {
  try {
    const r = await refreshDingtalk()
    if (r.ok) {
      if (r.already_running) {
        ElMessage.info('统计任务已在进行中，无需重复触发')
      } else {
        ElMessage.success('重新统计已启动：存储 → 知识数量 → 热门Top20 依次更新，全程约 1 小时')
      }
      setTimeout(loadAll, 3000)
      setTimeout(loadAll, 25000)
    } else {
      ElMessage.error(r.error || '刷新失败')
    }
  } catch (e: any) {
    ElMessage.error(e?.message || '刷新失败')
  }
}

// ============ 饼图 ============
const chartData = computed(() => {
  const data = onlyTop10.value ? distData.value.slice(0, 10) : distData.value
  return data.map((d, i) => ({ name: d.name, value: d.value, itemStyle: { color: COLORS[i % COLORS.length] } }))
})

function renderChart() {
  if (!chartRef.value) return
  if (!chartInstance) {
    chartInstance = echarts.init(chartRef.value)
  }
  chartInstance.resize()
  const option: echarts.EChartsCoreOption = {
    tooltip: {
      trigger: 'item',
      formatter: '{b}: {c} 份 ({d}%)',
    },
    legend: {
      orient: 'vertical',
      right: 10,
      top: 'center',
      itemWidth: 12,
      itemHeight: 12,
      textStyle: { fontSize: 12, color: '#475569' },
    },
    series: [
      {
        name: '企业知识数量',
        type: 'pie',
        radius: ['45%', '70%'],
        center: ['38%', '50%'],
        avoidLabelOverlap: true,
        itemStyle: { borderRadius: 0, borderColor: '#fff', borderWidth: 2 },
        label: { show: true, position: 'outside', formatter: '{b}', fontSize: 11, color: '#64748B' },
        labelLine: { show: true, length: 8, length2: 10 },
        data: chartData.value,
      },
    ],
  }
  chartInstance.setOption(option, true)
}

function renderGapChart() {
  if (!gapChartRef.value || !gapDepartments.value?.items.length) {
    gapChartInstance?.dispose()
    gapChartInstance = null
    return
  }
  if (gapChartInstance && gapChartInstance.getDom() !== gapChartRef.value) {
    gapChartInstance.dispose()
    gapChartInstance = null
  }
  if (!gapChartInstance) gapChartInstance = echarts.init(gapChartRef.value)
  gapChartInstance.resize()
  const rows = gapDepartments.value.items
  gapChartInstance.setOption({
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: any) => {
        const department = params?.[0]?.axisValue || ''
        const item = rows.find((row) => row.department === department)
        if (!item) return department
        return `${department}<br/>有内容文件夹：${item.covered_folder_count} 个<br/>知识缺失文件夹：${item.missing_folder_count} 个<br/>文件夹总数：${item.expected_folder_count} 个<br/>文件数：${item.uploaded_file_count} 个<br/>覆盖占比：${item.coverage_percent.toFixed(2)}%`
      },
    },
    legend: { top: 0, right: 4, itemWidth: 10, itemHeight: 10, textStyle: { color: '#475569', fontSize: 12 } },
    grid: { left: 132, right: 20, top: 32, bottom: 20 },
    xAxis: { type: 'value', minInterval: 1, axisLabel: { color: '#64748B' }, splitLine: { lineStyle: { color: '#E5E8EE' } } },
    yAxis: {
      type: 'category',
      inverse: true,
      data: rows.map((item) => item.department),
      axisLabel: { color: '#475569', width: 120, overflow: 'truncate' },
      axisLine: { show: false },
      axisTick: { show: false },
    },
    series: [
      { name: '有内容文件夹', type: 'bar', stack: 'folders', barMaxWidth: 18, itemStyle: { color: '#67C23A', borderRadius: [3, 0, 0, 3] }, data: rows.map((item) => item.covered_folder_count) },
      { name: '知识缺失文件夹', type: 'bar', stack: 'folders', barMaxWidth: 18, itemStyle: { color: '#E6A23C', borderRadius: [0, 3, 3, 0] }, data: rows.map((item) => item.missing_folder_count) },
    ],
  }, true)
}

function formatCoverage(row: KnowledgeGapDepartment) {
  return `${row.coverage_percent.toFixed(2)}%`
}

function gapSummary({ columns }: { columns: Array<{ property?: string }> }) {
  const total = gapDepartments.value?.total
  const values: Record<string, string> = {
    department: '合计',
    expected_folder_count: (total?.expected_folder_count || 0).toLocaleString(),
    missing_folder_count: (total?.missing_folder_count || 0).toLocaleString(),
    uploaded_file_count: (total?.uploaded_file_count || 0).toLocaleString(),
    coverage_percent: formatCoverage(total || {
      department: '合计', expected_folder_count: 0, missing_folder_count: 0,
      uploaded_file_count: 0, covered_folder_count: 0, coverage_percent: 0,
    }),
  }
  return columns.map((column) => column.property === 'department'
    ? values.department
    : column.property ? values[column.property] || '' : '')
}

// ============ 部门目录明细导出 ============
const gapTablePanelRef = ref<HTMLDivElement>()
const exporting = ref(false)

function downloadBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 60000)
}

// 导出表格：CSV（带 BOM，Excel 可直接打开），含合计行
function exportGapCsv() {
  if (!gapDepartments.value?.items.length) return
  const header = ['一级部门', '应上传文件夹数', '未上传文件夹数', '目前已上传文件数', '占比']
  const rows = gapDepartments.value.items.map((r) =>
    [r.department, r.expected_folder_count, r.missing_folder_count, r.uploaded_file_count, formatCoverage(r)])
  const total = gapDepartments.value.total
  if (total) {
    rows.push(['合计', total.expected_folder_count, total.missing_folder_count,
               total.uploaded_file_count, formatCoverage(total)])
  }
  const escapeCell = (c: string | number) =>
    /[",\n]/.test(String(c)) ? `"${String(c).replace(/"/g, '""')}"` : String(c)
  const csv = [header, ...rows].map((cells) => cells.map(escapeCell).join(',')).join('\n')
  downloadBlob(new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' }), '部门目录明细.csv')
  ElMessage.success('已导出表格')
}

// 导出图片：克隆面板并解除表格固定高度（表格内部滚动会截断数据行），截全量后下载 PNG
async function exportGapImage() {
  const panel = gapTablePanelRef.value
  if (!panel || exporting.value) return
  exporting.value = true
  let clone: HTMLElement | null = null
  try {
    clone = panel.cloneNode(true) as HTMLElement
    clone.style.position = 'fixed'
    clone.style.left = '-9999px'
    clone.style.top = '0'
    clone.style.width = `${panel.offsetWidth}px`
    // 释放 el-table 固定高度相关样式，让全部数据行自然展开
    const tableRoot = clone.querySelector('.el-table') as HTMLElement | null
    if (tableRoot) {
      tableRoot.style.height = 'auto'
      tableRoot.style.overflow = 'visible'
    }
    clone.querySelectorAll<HTMLElement>('.el-scrollbar__wrap').forEach((n) => { n.style.height = 'auto' })
    clone.querySelectorAll<HTMLElement>('.el-scrollbar__bar, .el-loading-mask').forEach((n) => { n.style.display = 'none' })
    document.body.appendChild(clone)
    const canvas = await html2canvas(clone, { scale: 2, backgroundColor: '#fff' })
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'))
    if (!blob) throw new Error('生成图片失败')
    downloadBlob(blob, '部门目录明细.png')
    ElMessage.success('已导出图片')
  } catch {
    ElMessage.error('导出图片失败，请重试')
  } finally {
    clone?.remove()
    exporting.value = false
  }
}

function onExportCommand(cmd: string | number | object) {
  if (cmd === 'csv') exportGapCsv()
  else if (cmd === 'image') exportGapImage()
}

// ============ 生命周期 ============
onMounted(() => {
  loadAll()
  window.addEventListener('resize', onWindowResize)
})
onUnmounted(() => {
  if (pollTimer) clearTimeout(pollTimer)
  window.removeEventListener('resize', onWindowResize)
  chartInstance?.dispose()
  gapChartInstance?.dispose()
})

// ============ 热门知识 Top20（钉钉知识库真实访问统计，后台逐文档扫描） ============
const hotData = ref<HotDocsResponse>({ items: [], scanned: 0, total: 0, loading: false, error: null, updated_at: null })

async function loadHot() {
  try {
    hotData.value = await getHotDocuments()
  } catch {
    /* 静默失败，随轮询重试 */
  }
}

// 知识运营成效 KPI（模拟）
const oldKpis = [
  { label: '问答准确率（北极星）', value: '87.6%', desc: '目标 95% · 较上周 +3.2pt', type: 'up' },
  { label: '活跃知识占比', value: '71%', desc: '1,052 份中 747 份活跃', type: '' },
  { label: '评测集通过率', value: '91.2%', desc: '486+62 题', type: 'up' },
  { label: 'Owner 响应及时率', value: '86%', desc: 'SLA 3 天', type: 'down' },
]

// 知识健康度分布（模拟）
const healthDist = [
  { level: '优秀', range: '≥90', count: 265, action: '保持 · 标杆知识', type: 'success' },
  { level: '良好', range: '80~89', count: 489, action: '正常运营', type: 'primary' },
  { level: '待改进', range: '60~79', count: 255, action: 'AI 生成整改建议 → Owner 确认', type: 'warning' },
  { level: '风险', range: '<60', count: 43, action: '暂停注册新应用 · 限期 14 天', type: 'danger' },
]

// Owner 贡献榜（模拟）
const ownerStats = [
  { owner: '王品控', contrib: '23 篇', cited: '1,204', fixed: '9', score: 318 },
  { owner: '张工', contrib: '18 篇', cited: '967', fixed: '6', score: 256 },
  { owner: '李售前', contrib: '12 篇', cited: '1,530', fixed: '3', score: 231 },
]
</script>

<template>
  <div class="kge-page dash-page">
    <div class="page-head">
      <h2 class="pg-title">运营看板</h2>
      <el-button size="small" :loading="loading" @click="loadAll">🔄 刷新</el-button>
    </div>

    <!-- 内容区：标题固定，其余内部滚动（kge-fill 约定） -->
    <div class="dash-body">
    <!-- 钉钉未配置 -->
    <div v-if="notConfigured" class="warn-bar">
      <el-icon><WarningFilled /></el-icon>
      <span>钉钉数据暂不可用：请在「系统配置」中填写钉钉 AppKey/AppSecret 与操作人 UnionId。</span>
      <el-button size="small" link type="primary" @click="doRefresh">重新拉取</el-button>
    </div>
    <!-- 知识数量遍历失败（存储权限不足不在这里提示，见存储卡片） -->
    <div v-else-if="distError && !distData.length && !distLoading" class="warn-bar">
      <el-icon><WarningFilled /></el-icon>
      <span>钉钉知识数量统计失败：{{ distError }}</span>
      <el-button size="small" link type="primary" @click="doRefresh">重新拉取</el-button>
    </div>
    <!-- 存储权限提示（知识数量正常、仅存储总量缺权限） -->
    <div v-else-if="storagePermMissing" class="warn-bar storage-warn">
      <el-icon><WarningFilled /></el-icon>
      <span>存储总量需在钉钉开放平台为应用开通「企业存储企业读权限（Storage.Org.Read）」后才与钉钉后台口径一致（含在线文档）；当前仅能统计上传文件大小。</span>
      <el-link type="primary" href="https://open-dev.dingtalk.com/appscope/apply?content=dingrsudkebucjlt1wv6%23Storage.Org.Read" target="_blank" :underline="false" style="font-size:13px;flex-shrink:0">
        去开通 →
      </el-link>
    </div>

    <!-- ============ 顶部指标卡 ============ -->
    <div class="metric-grid">
      <!-- 知识存储总量（企业钉盘用量，需 Storage.Org.Read 权限） -->
      <div class="metric-card">
        <div class="metric-icon" style="background:#3B82F6">
          <svg viewBox="0 0 1024 1024" width="22" height="22" fill="#fff"><path d="M512 128C288 128 128 176 128 256v512c0 80 160 128 384 128s384-48 384-128V256c0-80-160-128-384-128z m0 96c185 0 288 38 288 64s-103 64-288 64-288-38-288-64 103-64 288-64z m288 544c0 26-103 64-288 64s-288-38-288-64V448c62 42 168 64 288 64s226-22 288-64v320z"/></svg>
        </div>
        <div class="metric-body">
          <div class="metric-title">
            知识存储总量
            <el-tooltip content="企业存储全应用用量汇总（钉盘/在线文档/会议/AI听记等），与钉钉知识后台口径一致。优先企业存储 API；权限不足时自动走钉钉 CLI（用户扫码授权）兜底"><el-icon class="q"><QuestionFilled /></el-icon></el-tooltip>
          </div>
          <template v-if="overview?.storage.value != null">
            <div class="metric-value">{{ overview.storage.value }}<span class="unit">GB</span></div>
          </template>
          <template v-else-if="overview?.storage.loading">
            <div class="metric-value cs-value" style="font-size:18px">统计中…</div>
          </template>
          <template v-else>
            <div class="metric-value cs-value">未获取</div>
          </template>
        </div>
        <div class="metric-side">
          <el-tag v-if="storageFallback" size="small" type="warning" effect="plain" style="margin-bottom:6px">仅文件大小</el-tag>
          <el-tag v-else-if="storagePermMissing" size="small" type="danger" effect="plain">需开通权限</el-tag>
          <el-tooltip v-if="storageError" :content="storageError" placement="top">
            <el-icon class="warn-icon"><WarningFilled /></el-icon>
          </el-tooltip>
          <div v-if="storagePermMissing" class="sub" style="color:#DC2626;max-width:130px;line-height:1.4">请开通企业存储读权限</div>
        </div>
      </div>

      <!-- 知识数量（遍历钉钉知识库统计文件数） -->
      <div class="metric-card">
        <div class="metric-icon" style="background:#F59E0B">
          <svg viewBox="0 0 1024 1024" width="22" height="22" fill="#fff"><path d="M832 128H320c-70.7 0-128 57.3-128 128v512c0 70.7 57.3 128 128 128h448c70.7 0 128-57.3 128-128V256c0-70.7-57.3-128-128-128z m-448 64h384c35.3 0 64 28.7 64 64v64H320v-64c0-35.3 28.7-64 64-64z m384 576H320c-35.3 0-64-28.7-64-64V384h512v320c0 35.3-28.7 64-64 64z"/><path d="M128 256h64v512c0 35.3 28.7 64 64 64h64v64h-64c-70.7 0-128-57.3-128-128V256z"/></svg>
        </div>
        <div class="metric-body">
          <div class="metric-title">知识数量 <el-tooltip content="企业钉钉知识库中的文档/文件总数（首次加载需遍历全部知识库，约需数分钟）"><el-icon class="q"><QuestionFilled /></el-icon></el-tooltip></div>
          <div class="metric-value">
            <span v-if="countLoading" class="cs-value" style="font-size:18px">统计中…</span>
            <span v-else-if="overview?.knowledge_count.value != null">{{ Number(overview.knowledge_count.value).toLocaleString() }}</span>
            <span v-else class="cs-value" style="font-size:18px">—</span>
            <span class="unit">份</span>
          </div>
        </div>
        <div class="metric-side">
          <template v-if="overview?.knowledge_count.value != null">
            <div class="sub">本月新增：{{ Number(overview.knowledge_count.month_new || 0).toLocaleString() }}份</div>
            <el-tooltip v-if="overview.knowledge_count.partial"
              :content="`有 ${overview.knowledge_count.failed_folders} 个目录因钉钉限流未取到，计数可能偏小，可稍后点刷新重试`" placement="top">
              <el-tag size="small" type="warning" effect="plain" style="margin-top:6px">计数可能偏小</el-tag>
            </el-tooltip>
          </template>
        </div>
      </div>

      <!-- 知识召回数（待知识采集流程打通后统计） -->
      <div class="metric-card coming">
        <div class="metric-icon" style="background:#10B981">
          <svg viewBox="0 0 1024 1024" width="22" height="22" fill="#fff"><path d="M512 128c-212 0-384 172-384 384s172 384 384 384 384-172 384-384-172-384-384-384z m0 640c-141.4 0-256-114.6-256-256s114.6-256 256-256 256 114.6 256 256-114.6 256-256 256z"/><path d="M512 288c-123.7 0-224 100.3-224 224s100.3 224 224 224 224-100.3 224-224-100.3-224-224-224z m0 384c-88.4 0-160-71.6-160-160s71.6-160 160-160 160 71.6 160 160-71.6 160-160 160z"/><path d="M480 512h64v160h-64zM480 352h64v96h-64z"/></svg>
        </div>
        <div class="metric-body">
          <div class="metric-title">知识召回数 <el-tooltip content="知识采集流程打通、Dify 文档关联钉钉文档 ID 后统计"><el-icon class="q"><QuestionFilled /></el-icon></el-tooltip></div>
          <div class="metric-value cs-value">即将发布</div>
        </div>
        <div class="metric-side">
          <el-tag size="small" type="info" effect="plain">即将发布</el-tag>
        </div>
      </div>

      <!-- 调用量（跨整行，待知识采集流程打通后统计） -->
      <div class="metric-card full coming">
        <div class="metric-icon" style="background:#8B5CF6">
          <svg viewBox="0 0 1024 1024" width="22" height="22" fill="#fff"><path d="M416 384h192v64H416zM416 512h192v64H416zM416 640h128v64H416z"/><path d="M512 128C288 128 128 288 128 512s160 384 384 384 384-160 384-384S736 128 512 128z m0 640c-141.4 0-256-114.6-256-256s114.6-256 256-256 256 114.6 256 256-114.6 256-256 256z"/></svg>
        </div>
        <div class="metric-body">
          <div class="metric-title">调用量 <el-tooltip content="知识采集流程打通、Dify 文档关联钉钉文档 ID 后统计"><el-icon class="q"><QuestionFilled /></el-icon></el-tooltip></div>
          <div class="metric-value cs-value">即将发布</div>
        </div>
        <div class="metric-side">
          <el-tag size="small" type="info" effect="plain">即将发布</el-tag>
        </div>
      </div>
    </div>

    <!-- ============ 下半部分 ============ -->
    <div class="bottom-grid">
      <!-- 热门知识 Top20（钉钉知识库真实访问统计） -->
      <div class="panel">
        <div class="panel-head">
          <span class="panel-title">热门知识Top20 <el-tooltip content="按钉钉知识库文档访问次数倒序（访问数相同按阅读数排序）。逐文档调用钉钉统计接口，全量约 6700 个文档需 30 分钟左右，期间展示已统计部分，每日自动刷新"><el-icon class="q"><QuestionFilled /></el-icon></el-tooltip></span>
          <span v-if="hotData.loading" class="hot-progress">统计中 {{ hotData.scanned }}/{{ hotData.total || '…' }}</span>
          <span v-else-if="hotData.updated_at" class="hot-progress">更新于 {{ hotData.updated_at }}</span>
        </div>
        <div class="hot-list">
          <div v-if="hotData.error && !hotData.items.length" class="empty">{{ hotData.error }}（稍后自动重试）</div>
          <div v-else-if="!hotData.items.length" class="empty">
            <span v-if="hotData.loading && !hotData.total">正在准备文档清单（知识库遍历中）…</span>
            <span v-else>暂无数据。</span>
          </div>
          <div v-for="item in hotData.items" :key="item.rank" class="hot-item">
            <span class="rank" :class="{ top: item.rank <= 3 }">{{ item.rank }}</span>
            <a class="hot-title" :href="item.url || 'javascript:;'" target="_blank" rel="noopener" :title="`${item.title}（${item.workspace}）· 阅读 ${item.read_count} 次`">{{ item.title }}</a>
            <span class="hot-count">{{ item.count }}次</span>
          </div>
        </div>
      </div>

      <!-- 企业知识数量分布 -->
      <div class="panel">
        <div class="panel-head">
          <span class="panel-title">企业知识数量 <el-tooltip content="各钉钉知识库的文件数量分布"><el-icon class="q"><QuestionFilled /></el-icon></el-tooltip></span>
          <div class="panel-tools">
            <el-select v-model="onlyTop10" size="small" style="width:120px">
              <el-option :value="true" label="仅展示Top10" />
              <el-option :value="false" label="展示全部" />
            </el-select>
          </div>
        </div>
        <div v-show="distData.length" ref="chartRef" class="chart"></div>
        <div v-if="!distData.length" class="empty">
          <span v-if="distLoading">正在遍历钉钉知识库统计，约需数分钟…</span>
          <span v-else>暂无数据。</span>
        </div>
      </div>
    </div>

    <!-- ============ 部门知识库上传进度（来自知识缺口目录快照） ============ -->
    <div class="section-title gap-section-title">
      <span>部门知识库上传进度</span>
      <span v-if="gapDepartments?.updated_at" class="snapshot-time">目录快照更新于 {{ gapDepartments.updated_at }}</span>
    </div>
    <div class="gap-grid">
      <div class="panel gap-chart-panel" v-loading="gapLoading">
        <div class="panel-head">
          <span class="panel-title">部门目录覆盖情况</span>
          <el-tooltip content="按知识库名称汇总；文件夹数量或文件数任一大于 0 即视为有内容"><el-icon class="q"><QuestionFilled /></el-icon></el-tooltip>
        </div>
        <div v-if="gapError" class="gap-state"><el-empty :description="gapError" /></div>
        <div v-else-if="!gapDepartments?.has_snapshot || !gapDepartments.items.length" class="gap-state">
          <el-empty :description="gapDepartments?.has_snapshot ? '目录快照中暂无一级部门明细' : '暂无目录快照，请先到「知识缺口」刷新钉钉知识库目录'" />
        </div>
        <div v-else ref="gapChartRef" class="gap-chart"></div>
      </div>
      <div ref="gapTablePanelRef" class="panel gap-table-panel" v-loading="gapLoading">
        <div class="panel-head">
          <div class="head-left">
            <span class="panel-title">部门目录明细</span>
            <el-tooltip content="占比 = 有内容文件夹数 ÷ 文件夹总数；有内容指文件夹数量或文件数任一大于 0"><el-icon class="q"><QuestionFilled /></el-icon></el-tooltip>
          </div>
          <el-dropdown v-if="gapDepartments?.has_snapshot && gapDepartments.items.length" placement="bottom-end" @command="onExportCommand">
            <el-button size="small" :icon="Download" :loading="exporting">导出<el-icon class="el-icon--right"><ArrowDown /></el-icon></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="csv">导出表格（CSV）</el-dropdown-item>
                <el-dropdown-item command="image">导出图片（PNG）</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
        <el-table v-if="gapDepartments?.has_snapshot && gapDepartments.items.length" :data="gapDepartments.items" border size="small" height="380" class="gap-table" show-summary :summary-method="gapSummary">
          <el-table-column prop="department" label="一级部门" min-width="130" show-overflow-tooltip />
          <el-table-column prop="expected_folder_count" label="应上传文件夹数" width="126" align="center" />
          <el-table-column prop="missing_folder_count" label="未上传文件夹数" width="126" align="center" />
          <el-table-column prop="uploaded_file_count" label="目前已上传文件数" width="136" align="center" />
          <el-table-column prop="coverage_percent" label="占比" width="100" align="right">
            <template #default="{ row }">{{ formatCoverage(row as KnowledgeGapDepartment) }}</template>
          </el-table-column>
        </el-table>
        <div v-else-if="gapError" class="gap-state"><el-empty :description="gapError" /></div>
        <div v-else class="gap-state"><el-empty description="暂无部门目录明细" /></div>
      </div>
    </div>

    <!-- ============ 知识运营成效（模拟数据） ============ -->
    <div class="section-title">
      <span>知识运营成效</span>
      <el-tag size="small" type="warning" effect="plain">示例数据</el-tag>
    </div>

    <el-row :gutter="16" class="kpi-row">
      <el-col :span="6" v-for="k in oldKpis" :key="k.label">
        <div class="kpi">
          <div class="l">{{ k.label }}</div>
          <div class="v">{{ k.value }}</div>
          <div class="d" :class="k.type">{{ k.desc }}</div>
        </div>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>知识健康度分布</span>
              <el-tag size="small" type="warning" effect="plain">示例数据</el-tag>
            </div>
          </template>
          <el-table :data="healthDist" style="width: 100%">
            <el-table-column label="健康度" width="100">
              <template #default="{ row }"><el-tag :type="row.type as any">{{ row.level }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="range" label="分数段" width="100" />
            <el-table-column prop="count" label="文档数" width="100" align="center" />
            <el-table-column label="运营动作">
              <template #default="{ row }"><span class="small">{{ row.action }}</span></template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>Owner 贡献榜（本月）</span>
              <el-tag size="small" type="warning" effect="plain">示例数据</el-tag>
            </div>
          </template>
          <el-table :data="ownerStats" style="width: 100%">
            <el-table-column prop="owner" label="Owner" width="100" />
            <el-table-column prop="contrib" label="贡献" width="90" align="center" />
            <el-table-column prop="cited" label="被引用" width="90" align="center" />
            <el-table-column prop="fixed" label="纠错修复" width="90" align="center" />
            <el-table-column label="积分" width="90" align="center">
              <template #default="{ row }"><b>{{ row.score }}</b></template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
    </div>
  </div>
</template>

<style scoped>
/* kge-page 为 flex 纵向容器：标题行固定，内容区撑满剩余高度并内部滚动 */
.dash-page .page-head { flex-shrink: 0; }
.dash-body { flex: 1; min-height: 0; overflow-y: auto; }
.page-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }
.pg-title { font-size: 19px; margin: 0; }
.warn-bar { display: flex; align-items: center; gap: 8px; background: #FFFBEB; border: 1px solid #FDE68A; color: #92400E; padding: 8px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 16px; }
.warn-bar .el-icon { color: #F59E0B; }
.warn-bar.storage-warn { background: #EFF6FF; border-color: #BFDBFE; color: #1E40AF; }
.warn-bar.storage-warn .el-icon { color: #3B82F6; }

/* 指标卡 */
.metric-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 16px; }
.metric-card { background: #fff; border: 1px solid #E5E8EE; border-radius: 12px; padding: 18px 20px; display: flex; align-items: center; gap: 14px; box-shadow: 0 1px 3px rgba(16,24,40,.06); }
.metric-card.full { grid-column: 1 / -1; }
.metric-icon { width: 44px; height: 44px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.metric-body { flex: 1; min-width: 0; }
.metric-title { font-size: 14px; color: #1F2937; font-weight: 600; display: flex; align-items: center; gap: 4px; margin-bottom: 6px; }
.metric-title .q { color: #9CA3AF; font-size: 14px; cursor: help; }
.metric-value { font-size: 30px; font-weight: 700; color: #111827; line-height: 1.1; }
.metric-value .unit { font-size: 15px; font-weight: 500; color: #6B7280; margin-left: 4px; }
.metric-side { text-align: right; flex-shrink: 0; }
.mom { display: inline-block; font-size: 12.5px; padding: 2px 8px; border-radius: 4px; font-weight: 500; }
.sub { font-size: 12.5px; color: #6B7280; margin-top: 6px; }

/* 下方面板 */
.bottom-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.panel { background: #fff; border: 1px solid #E5E8EE; border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 3px rgba(16,24,40,.06); }
.panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.panel-head .head-left { display: flex; align-items: center; gap: 4px; min-width: 0; }
.panel-title { font-size: 15px; font-weight: 600; color: #1F2937; display: flex; align-items: center; gap: 4px; }
.panel-title .q { color: #9CA3AF; font-size: 14px; cursor: help; }
.panel-tools { display: flex; align-items: center; gap: 8px; }

/* 热门列表 */
.hot-list { max-height: 460px; overflow-y: auto; }
.hot-item { display: flex; align-items: center; gap: 12px; padding: 10px 4px; border-bottom: 1px solid #F1F5F9; }
.hot-item:last-child { border-bottom: none; }
.rank { width: 22px; height: 22px; border-radius: 5px; background: #E5E7EB; color: #6B7280; font-size: 12px; font-weight: 600; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.rank.top { color: #fff; }
.rank.top:nth-child(1 of .rank), .hot-item:nth-child(1) .rank { background: #EF4444; }
.hot-item:nth-child(2) .rank { background: #F97316; }
.hot-item:nth-child(3) .rank { background: #F97316; }
.hot-title { flex: 1; font-size: 13.5px; color: #374151; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
a.hot-title { text-decoration: none; cursor: pointer; }
a.hot-title:hover { color: #2563EB; }
.hot-progress { font-size: 12px; color: #6B7280; flex-shrink: 0; margin-left: auto; }
.hot-count { font-size: 13px; color: #6B7280; flex-shrink: 0; }

.chart { width: 100%; height: 380px; }
.empty { text-align: center; color: #9CA3AF; font-size: 13px; padding: 40px 0; }
.gap-section-title { margin-top: 24px; }
.snapshot-time { margin-left: auto; font-size: 12px; font-weight: 400; color: #909399; }
.gap-grid { display: grid; grid-template-columns: minmax(0, .9fr) minmax(0, 1.1fr); gap: 16px; }
.gap-chart-panel, .gap-table-panel { min-width: 0; }
.gap-chart { width: 100%; height: 380px; }
.gap-state { height: 380px; display: flex; align-items: center; justify-content: center; }
.gap-table :deep(.el-table__footer-wrapper td) { background: #F8FAFC; font-weight: 600; color: #1F2937; }

/* 原运营指标（示例数据） */
.section-title { display: flex; align-items: center; gap: 10px; margin: 28px 0 14px; font-size: 16px; font-weight: 600; color: #1F2937; }
.kpi-row { margin-bottom: 16px; }
.kpi { background: #fff; border: 1px solid #E5E8EE; border-radius: 12px; padding: 14px 16px; box-shadow: 0 1px 3px rgba(16,24,40,.06); }
.kpi .l { font-size: 12px; color: #6b7280; margin-bottom: 6px; }
.kpi .v { font-size: 24px; font-weight: 700; }
.kpi .d { font-size: 11.5px; margin-top: 4px; color: #6b7280; }
.kpi .d.up { color: #16a34a; }
.kpi .d.down { color: #dc2626; }
.card-header { display: flex; align-items: center; justify-content: space-between; }
.small { font-size: 12.5px; color: #475569; line-height: 1.8; }

/* 顶部指标卡「即将发布」 */
.metric-card.coming { opacity: 0.8; }
.cs-value { font-size: 20px !important; color: #9CA3AF !important; font-weight: 500 !important; letter-spacing: 1px; }

@media (max-width: 900px) {
  .metric-grid { grid-template-columns: 1fr; }
  .bottom-grid { grid-template-columns: 1fr; }
  .gap-grid { grid-template-columns: 1fr; }
}
</style>
