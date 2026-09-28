<script setup lang="ts">
import { ref, onMounted, nextTick, computed } from 'vue'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'
import {
  getOverview,
  getKnowledgeDistribution,
  getHotDocuments,
  refreshDingtalk,
  type OverviewResponse,
  type DistributionItem,
  type HotDocsResponse,
} from '@/api/operate'

// ============ 数据 ============
const loading = ref(false)
const overview = ref<OverviewResponse | null>(null)
const distData = ref<DistributionItem[]>([])
const distError = ref('')
const distLoading = ref(false)
const onlyTop10 = ref(true)
const chartRef = ref<HTMLDivElement>()
let chartInstance: echarts.ECharts | null = null
let pollTimer: ReturnType<typeof setTimeout> | null = null

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
    const [ov, dist] = await Promise.all([getOverview(), getKnowledgeDistribution(), loadHot()])
    overview.value = ov
    distData.value = dist.items || []
    distLoading.value = !!dist.loading
    // 知识数量/分布的错误与存储权限错误分开：存储权限错误只在存储卡片上提示
    distError.value = dist.error || ''
    await nextTick()
    renderChart()
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

// ============ 生命周期 ============
onMounted(() => {
  loadAll()
  window.addEventListener('resize', () => chartInstance?.resize())
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
}
</style>
