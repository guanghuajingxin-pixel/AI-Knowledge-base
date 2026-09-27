<script setup lang="ts">
/**
 * 解析引擎 · 页签一【自定义解析】
 *
 * 双引擎接入，页面交互（上传 / 队列 / 结果预览）完全复用，按 V1 形状发起请求：
 *  - GET  /v1/health                 健康检查（版本 / 支持的输出格式）
 *  - GET  /v1/tiers                  解析档位
 *  - POST /v1/uploads                创建上传会话
 *  - PUT  /v1/uploads/{id}/content   上传原始字节（octet-stream）
 *  - POST /v1/uploads/{id}/complete  完成上传 → 得到 file_id
 *  - POST /v1/parse/jobs             创建解析任务（file_id 源，可多文件）
 *  - GET  /v1/parse/jobs/{id}        轮询状态（status / progress / output_files）
 *  - GET  /v1/files/{id}/content     下载解析产物（markdown / structured_json）
 *  - DELETE /v1/parse/jobs/{id}      取消排队 / 运行中的任务
 *
 * 引擎切换（X-Mineru-Base 头）：
 *  - local：本地 mineru-kit V1 服务（Base URL 用户配置，无鉴权），代理原样透传；
 *  - cloud：MinerU 云端 SaaS（mineru.net/api/v4），后端适配器把 V1 形状翻译为
 *    V4 契约（file-urls/batch 预签名上传 + extract-results/batch 轮询 + 结果 zip），
 *    Key 复用模型配置页 mineru_api_key，前端不经手密钥；云端任务不支持取消。
 *
 * 浏览器直连 MinerU 会被 CORS 拦截（服务不带 CORS 头，官方 WebUI 靠同源），
 * 故全部请求经 kb-api 同源代理 /api/v1/mineru/*，目标由 X-Mineru-Base 头指定。
 * 作为子组件被 ProcessEngine.vue（页签外壳）引用；页签二为 MinerU 官方 WebUI（iframe）。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox, type UploadInstance } from 'element-plus'
import { marked } from 'marked'
import request from '@/api/request'
import {
  Connection,
  Delete,
  DocumentCopy,
  Download,
  QuestionFilled,
  Refresh,
  Search,
  Upload,
  UploadFilled,
} from '@element-plus/icons-vue'

// ============================================================
// 类型（对齐 MinerU V1 OpenAPI 规范）
// ============================================================
type JobStatus = 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'canceled'
type FileStatus = 'queued' | 'running' | 'completed' | 'failed'
type OcrMode = 'auto' | 'txt' | 'ocr'

interface TierInfo {
  id: string
  description: string
  current_model?: string | null
}

interface HealthInfo {
  status?: string
  version?: string
  features?: { output_formats?: string[]; sources?: string[] }
}

interface FileObject {
  id: string
  filename: string
  bytes: number
}

interface UploadResponse {
  id: string
  status: string
  upload_url?: string | null
  upload_headers?: Record<string, string> | null
  file?: FileObject | null
}

interface OutputFileRef {
  file_id: string
  bytes: number
}

interface FileParseInfo {
  model_used?: string | null
  duration_ms?: number | null
  parser_version?: string | null
}

interface JobFileResult {
  file_id?: string | null
  name: string
  page_range?: string
  status: FileStatus
  parse?: FileParseInfo | null
  output_files?: {
    markdown?: OutputFileRef | null
    structured_json?: OutputFileRef | null
    [k: string]: OutputFileRef | null | undefined
  } | null
  error?: { message?: string; detail?: string } | null
}

interface JobAsyncResponse {
  job_id: string
  status: JobStatus
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  tier?: string
  output_formats?: string[]
  progress?: { completed: number; failed: number; total: number }
  files: JobFileResult[]
}

interface JobListItem {
  job_id: string
  status: JobStatus
  created_at: string
  file_count: number
}

/** 队列内任务（本地视图模型：响应 + 懒加载的产物内容） */
interface QueueJob {
  job_id: string
  status: JobStatus
  created_at: string
  tier?: string
  file_count: number
  progress?: { completed: number; failed: number; total: number }
  files?: JobFileResult[]
  /** 懒加载：markdown / structured_json 文本，key = `${jobId}:${fileIndex}` */
  contents: Record<string, { md?: string; json?: string }>
}

// ============================================================
// 连接配置（localStorage 持久化）
// 引擎二选一：local=本地 mineru-kit（Base URL，无鉴权）；cloud=MinerU 云端 SaaS
// （mineru.net/api/v4，Key 走模型配置页 mineru_api_key，前端不经手密钥）
// ============================================================
const CONFIG_KEY = 'kge:mineru_config_v1'
const DEFAULT_BASE = 'http://127.0.0.1:8010'
// 默认引擎：cloud。生产环境（如 10.10.166.2）本地 mineru-kit 通常未部署，
// 默认 local 会让首次进入页面的健康检查/上传全 502；改为 cloud 后，未显式配置
// 过的用户直接走 SaaS（Key 由「模型配置」页 mineru_api_key 提供，前端不经手）。
const DEFAULT_ENGINE: 'local' | 'cloud' = 'cloud'

const config = reactive<{ baseUrl: string; engine: 'local' | 'cloud' }>({
  baseUrl: DEFAULT_BASE,
  engine: DEFAULT_ENGINE,
})

function loadConfig() {
  try {
    const raw = localStorage.getItem(CONFIG_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as { baseUrl?: string; engine?: string }
      config.baseUrl = parsed.baseUrl?.trim() || DEFAULT_BASE
      // 只有显式存过 'local' 才回到本地引擎；其余（含历史默认值缺失）走 cloud
      config.engine = parsed.engine === 'local' ? 'local' : 'cloud'
    }
  } catch {
    /* 忽略损坏的本地缓存 */
  }
}

function saveConfig() {
  try {
    localStorage.setItem(CONFIG_KEY, JSON.stringify({ baseUrl: config.baseUrl, engine: config.engine }))
  } catch {
    /* 存储失败不影响使用 */
  }
}

// Base URL / 引擎变更即时持久化：页签外壳的【OpenAPI】入口读取同一份配置，需保持最新
watch([() => config.baseUrl, () => config.engine], saveConfig)

const normalizedBase = computed(() => config.baseUrl.trim().replace(/\/+$/, ''))

// ============================================================
// 顶部提示 / 健康检查
// ============================================================
type BannerType = 'info' | 'success' | 'warning' | 'error'

// 顶部水滴提示：ElMessage 悬浮于页面顶部，3 秒后自动消失
function setBanner(type: BannerType, message: string) {
  ElMessage({ type, message, duration: 3000 })
}

const health = reactive({
  ok: false,
  checking: false,
  info: null as HealthInfo | null,
  error: '',
})

// 连接状态轻提示：圆点角标挂「连接测试」按钮右上角（绿=连通，红=失败，未测试不显示）
const dotClass = computed(() => {
  if (health.ok) return 'is-success'
  if (health.error) return 'is-danger'
  return ''
})
const dotTip = computed(() =>
  health.ok ? '连接正常' : `连接失败：${health.error || '未知错误'}`,
)
// 引擎说明收敛为 hover 提示，双模式占位等宽避免行宽跳动
const engineHint = computed(() =>
  config.engine === 'cloud'
    ? '云端 SaaS：直连 mineru.net/api/v4，API Key 取「模型配置」页的 MinerU API Key'
    : '本地服务：对接 mineru-kit V1 API（无鉴权），地址即上方 Base URL',
)

// ============================================================
// 通用请求封装（经 kb-api 同源代理 /api/v1/mineru/*，规避 CORS）
// ============================================================
interface ApiOpts {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH'
  body?: unknown
  headers?: Record<string, string>
  timeout?: number
  responseType?: 'blob'
}

async function api<T>(path: string, opts: ApiOpts = {}): Promise<T> {
  // cloud 模式目标固定为 "cloud"（后端适配器），本地模式为用户配置的 Base URL
  const target = config.engine === 'cloud' ? 'cloud' : normalizedBase.value
  if (!target) throw new Error('未配置 MinerU 服务地址')
  // 相对路径 → 走代理；绝对地址（upload_url）→ 取其 path 部分走代理
  const cleanPath = path.replace(/^https?:\/\/[^/]+/i, '').replace(/^\/+/, '')
  const headers: Record<string, string> = { 'X-Mineru-Base': target, ...(opts.headers || {}) }
  // axios 对字符串 body 不会自动设 Content-Type（浏览器默认 text/plain 会被 MinerU 以 400 拒绝）
  if (typeof opts.body === 'string' && !Object.keys(headers).some((k) => k.toLowerCase() === 'content-type')) {
    headers['Content-Type'] = 'application/json'
  }
  const res = await request({
    url: `/mineru/${cleanPath}`,
    method: opts.method || 'GET',
    data: opts.body as never,
    headers,
    timeout: opts.timeout,
    responseType: opts.responseType,
  })
  return res as T
}

/** 统一错误文案：优先取代理（detail）/ MinerU（error.message）返回的具体原因 */
function errText(e: unknown): string {
  const ax = e as {
    response?: { data?: { detail?: unknown; message?: unknown; error?: { message?: unknown } } }
  }
  const d = ax?.response?.data
  const detail = d?.detail ?? d?.message ?? d?.error?.message
  if (typeof detail === 'string' && detail) return detail
  return e instanceof Error ? e.message : String(e)
}

async function runHealthCheck(silent = false) {
  if (config.engine === 'local' && !normalizedBase.value) {
    health.ok = false
    health.error = '缺少服务地址'
    if (!silent) setBanner('warning', '请先填写 MinerU 服务 Base URL（默认 http://127.0.0.1:8010）')
    return false
  }
  health.checking = true
  try {
    const d = await api<HealthInfo>('/v1/health')
    health.ok = true
    health.info = d
    health.error = ''
    if (!silent) {
      const fmts = d.features?.output_formats?.join(' / ') || '—'
      setBanner('success', `MinerU 服务已连接（v${d.version || '?'}），支持输出：${fmts}`)
    }
    return true
  } catch (e) {
    const msg = errText(e)
    health.ok = false
    health.info = null
    health.error = msg
    if (!silent) setBanner('error', `MinerU 服务不可达：${msg}　请确认服务已启动、地址正确。`)
    return false
  } finally {
    health.checking = false
  }
}

async function handleTestConn() {
  saveConfig()
  const ok = await runHealthCheck(false)
  if (ok) {
    await loadTiers()
    await syncServerJobs(true)
  }
}

/** 切换引擎（本地/云端）：重置档位并按新模式重测连接 */
async function handleEngineChange() {
  saveConfig()
  tiers.value = []
  health.ok = false
  health.info = null
  const ok = await runHealthCheck(true)
  if (ok) await loadTiers()
}

// ============================================================
// 解析档位（/v1/tiers 动态拉取）
// ============================================================
const tiers = ref<TierInfo[]>([])
const tiersLoading = ref(false)

const TIER_FALLBACK: TierInfo[] = [
  { id: 'speed', description: '速度优先：最快出结果，适合预览与批量草稿', current_model: 'flash' },
  { id: 'balanced', description: '均衡：质量与速度折中，推荐默认档位', current_model: 'MinerU2.5' },
  { id: 'quality', description: '效果最佳：解析最准确，速度较慢', current_model: 'MinerU2.5' },
]

async function loadTiers() {
  tiersLoading.value = true
  try {
    const d = await api<{ data?: TierInfo[] } | TierInfo[]>('/v1/tiers')
    const list = Array.isArray(d) ? d : d?.data || []
    if (list.length) tiers.value = list
  } catch {
    /* 拉取失败保留现有选项 */
  } finally {
    tiersLoading.value = false
  }
}

// ============================================================
// 上传区（多文件 → 一个解析任务）
// ============================================================
const uploadRef = ref<UploadInstance>()
const dropHover = ref(false)
const selectedFiles = ref<File[]>([])
const uploading = ref(false)

const ACCEPT_EXT = '.pdf,.png,.jpg,.jpeg'
const MAX_FILE_MB = 200

const OCR_OPTIONS: Array<{ value: OcrMode; label: string; desc: string }> = [
  { value: 'auto', label: 'auto（自动）', desc: '自动判断是否走 OCR' },
  { value: 'txt', label: 'txt（原生文本）', desc: '仅提取内嵌文字层' },
  { value: 'ocr', label: 'ocr（强制 OCR）', desc: '扫描件 / 图片型 PDF' },
]

const options = reactive({
  tier: '' as string,
  ocrMode: 'auto' as OcrMode,
  pageRange: '',
})

function handleFilePick(file: { raw?: File } | undefined | null) {
  const f = file?.raw
  if (!f) return
  if (!/\.(pdf|png|jpe?g)$/i.test(f.name)) {
    ElMessage.error('MinerU 仅支持 PDF / PNG / JPG')
    return
  }
  if (f.size > MAX_FILE_MB * 1024 * 1024) {
    ElMessage.error(`文件超过 ${MAX_FILE_MB} MB 上限`)
    return
  }
  selectedFiles.value.push(f)
}

function removeFile(idx: number) {
  selectedFiles.value.splice(idx, 1)
}

function clearFiles() {
  selectedFiles.value = []
  uploadRef.value?.clearFiles()
}

function fmtSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`
  return `${(bytes / 1024).toFixed(1)} KB`
}

/** 三步上传：创建会话 → PUT 字节 → complete → file_id */
async function uploadOne(file: File): Promise<string> {
  const created = await api<UploadResponse>('/v1/uploads', {
    method: 'POST',
    body: JSON.stringify({
      filename: file.name,
      bytes: file.size,
      mime_type: file.type || 'application/octet-stream',
    }),
  })
  await api(`/v1/uploads/${created.id}/content`, {
    method: 'PUT',
    headers: { 'Content-Type': file.type || 'application/octet-stream' },
    body: file,
    timeout: 300000,
  })
  const done = await api<UploadResponse>(`/v1/uploads/${created.id}/complete`, {
    method: 'POST',
    body: '{}',
  })
  if (!done.file?.id) throw new Error('上传完成但未返回 file_id')
  return done.file.id
}

async function submitParse() {
  if (!selectedFiles.value.length) return
  if (config.engine === 'local' && !normalizedBase.value) {
    setBanner('warning', '请先配置 MinerU 服务地址并测试连接')
    return
  }
  uploading.value = true
  const fileIds: string[] = []
  try {
    for (const f of selectedFiles.value) {
      fileIds.push(await uploadOne(f))
    }
    const body: Record<string, unknown> = {
      files: fileIds.map((file_id) => ({ source: { type: 'file_id', file_id } })),
      ocr_mode: options.ocrMode,
      output_formats: ['markdown', 'structured_json'],
    }
    if (options.tier) body.tier = options.tier
    if (options.pageRange.trim()) {
      body.files = (body.files as Array<{ source: unknown }>).map((f, i) => ({
        ...f,
        page_range: i === 0 ? options.pageRange.trim() : undefined,
      }))
    }
    const d = await api<JobAsyncResponse>('/v1/parse/jobs', {
      method: 'POST',
      body: JSON.stringify(body),
    })
    addJob({
      job_id: d.job_id,
      status: d.status,
      created_at: d.created_at,
      tier: d.tier,
      file_count: d.files?.length || fileIds.length,
      progress: d.progress,
      files: d.files,
      contents: {},
    })
    startPoll(d.job_id)
    setBanner('success', `已提交任务，共 ${fileIds.length} 个文件，进入解析队列`)
    activeInnerTab.value = 'queue' // 提交后自动切到队列页签跟踪进度
    clearFiles()
  } catch (e) {
    setBanner('error', `提交失败：${errText(e)}`)
  } finally {
    uploading.value = false
  }
}

// ============================================================
// 任务队列（本地 Map + 2s 轮询）
// ============================================================
const jobs = ref<Map<string, QueueJob>>(new Map())
const pollTimers = new Map<string, number>()

const jobRows = computed(() =>
  Array.from(jobs.value.values()).sort((a, b) => (a.created_at < b.created_at ? 1 : -1)),
)

const queueStats = computed(() => {
  const rows = jobRows.value
  return {
    total: rows.length,
    running: rows.filter((j) => !isTerminal(j.status)).length,
    completed: rows.filter((j) => j.status === 'completed' || j.status === 'partial').length,
    failed: rows.filter((j) => j.status === 'failed' || j.status === 'canceled').length,
  }
})

/** 内部页签：解析测试 / 解析队列 */
const activeInnerTab = ref<'test' | 'queue'>('test')

/** 队列筛选：状态 + 文档名称 / 任务 ID 关键字 */
const queueFilter = reactive({ status: '' as string, keyword: '' })

/** 任务内全部文档名（多文件顿号连接，供筛选与列显示） */
function jobFileNames(j: QueueJob): string {
  return (j.files || []).map((f) => f.name).filter(Boolean).join('，')
}

const EXT_TYPE: Record<string, string> = {
  pdf: 'PDF', png: 'PNG', jpg: 'JPG', jpeg: 'JPG', jpe: 'JPG',
  doc: 'DOC', docx: 'DOCX', xls: 'XLS', xlsx: 'XLSX', ppt: 'PPT', pptx: 'PPTX',
  html: 'HTML', htm: 'HTML', txt: 'TXT', md: 'MD', csv: 'CSV',
}

/** 文档类型（扩展名映射，多文件去重） */
function jobFileTypes(j: QueueJob): string[] {
  const s = new Set<string>()
  for (const f of j.files || []) {
    const m = /\.([a-z0-9]+)$/i.exec(f.name || '')
    if (m) s.add(EXT_TYPE[m[1].toLowerCase()] || m[1].toUpperCase())
  }
  return Array.from(s)
}

/** 任务内各文档的产物文件 ID（markdown 优先；GET /v1/files/{id}/content 可下载。
 *  注：MinerU 无鉴权模式下源文件禁止下载（403），仅产物可下载） */
function jobFileIds(j: QueueJob): Array<{ id: string; name: string }> {
  return (j.files || [])
    .map((f) => ({
      id: f.output_files?.markdown?.file_id || f.output_files?.structured_json?.file_id || '',
      name: f.name || '',
    }))
    .filter((x) => x.id)
}

const filteredQueue = computed(() =>
  jobRows.value.filter((j) => {
    if (queueFilter.status && j.status !== queueFilter.status) return false
    const kw = queueFilter.keyword.trim().toLowerCase()
    if (kw) {
      const hay = `${jobFileNames(j)} ${j.job_id}`.toLowerCase()
      if (!hay.includes(kw)) return false
    }
    return true
  }),
)

function isTerminal(st: string): boolean {
  return st === 'completed' || st === 'partial' || st === 'failed' || st === 'canceled'
}

function addJob(j: QueueJob) {
  jobs.value.set(j.job_id, j)
  jobs.value = new Map(jobs.value)
}

/** 连续失败计数：服务不可达时熔断轮询，避免错误消息刷屏 */
const pollFailures = new Map<string, number>()

function startPoll(id: string) {
  if (pollTimers.has(id)) return
  pollFailures.set(id, 0)
  const tick = window.setInterval(() => pollJob(id), 2000)
  pollTimers.set(id, tick)
  pollJob(id)
}
function stopPoll(id: string) {
  const t = pollTimers.get(id)
  if (t) {
    window.clearInterval(t)
    pollTimers.delete(id)
  }
}
function stopAllPolls() {
  pollTimers.forEach((t) => window.clearInterval(t))
  pollTimers.clear()
}

/** 轮询单个任务：状态 / 进度 / 产物引用（连续失败 3 次熔断） */
async function pollJob(id: string) {
  const j = jobs.value.get(id)
  if (!j) return
  try {
    const d = await api<JobAsyncResponse>(`/v1/parse/jobs/${encodeURIComponent(id)}`)
    pollFailures.set(id, 0)
    Object.assign(j, {
      status: d.status,
      tier: d.tier,
      progress: d.progress,
      files: d.files,
      file_count: d.files?.length || j.file_count,
    })
    jobs.value = new Map(jobs.value)
    if (isTerminal(d.status)) stopPoll(id)
  } catch {
    const n = (pollFailures.get(id) || 0) + 1
    pollFailures.set(id, n)
    if (n >= 3) {
      stopPoll(id)
      setBanner('error', `任务 ${shortId(id)} 轮询连续失败 ${n} 次，已暂停刷新；可点「从服务端拉取」恢复。`)
    }
  }
}

/** 从服务端拉取最近任务列表（合并进本地视图；已结束任务补拉详情以显示文档名称/类型） */
async function syncServerJobs(silent = false) {
  if (!normalizedBase.value) {
    if (!silent) setBanner('warning', '请先配置 MinerU 服务地址')
    return
  }
  try {
    const d = await api<{ data?: JobListItem[] } | JobListItem[]>('/v1/parse/jobs')
    const list = Array.isArray(d) ? d : d?.data || []
    const needDetail: QueueJob[] = []
    list.forEach((it) => {
      if (!jobs.value.has(it.job_id)) {
        const j: QueueJob = {
          job_id: it.job_id,
          status: it.status,
          created_at: it.created_at,
          file_count: it.file_count,
          contents: {},
        }
        addJob(j)
        if (!isTerminal(it.status)) {
          startPoll(it.job_id) // 轮询首轮即拉详情
        } else {
          needDetail.push(j) // 列表接口无 files，补拉一次详情
        }
      }
    })
    // 已结束任务批量拉详情（6 并发一批，单个失败不影响列表）
    for (let i = 0; i < needDetail.length; i += 6) {
      await Promise.all(
        needDetail.slice(i, i + 6).map(async (j) => {
          try {
            const det = await api<JobAsyncResponse>(`/v1/parse/jobs/${encodeURIComponent(j.job_id)}`)
            Object.assign(j, { status: det.status, tier: det.tier, files: det.files, progress: det.progress })
          } catch {
            /* 详情拉取失败：文档名称/类型列显示占位 */
          }
        }),
      )
    }
    if (needDetail.length) jobs.value = new Map(jobs.value)
    if (!silent) ElMessage.success(`已从服务端合并 ${list.length} 条任务`)
  } catch (e) {
    if (!silent) {
      setBanner('error', `拉取任务列表失败：${errText(e)}`)
    }
  }
}

async function handleCancelJob(id: string) {
  try {
    await ElMessageBox.confirm(`取消任务 ${id}？进行中的解析将中止。`, '取消任务', {
      type: 'warning',
      confirmButtonText: '取消任务',
      cancelButtonText: '返回',
    })
  } catch {
    return
  }
  try {
    await api(`/v1/parse/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' })
    stopPoll(id)
    await pollJob(id)
    ElMessage.success('已取消')
  } catch (e) {
    ElMessage.error(`取消失败：${errText(e)}`)
  }
}

function handleRemoveJob(id: string) {
  stopPoll(id)
  jobs.value.delete(id)
  jobs.value = new Map(jobs.value)
  if (currentJobId.value === id) {
    currentJobId.value = null
    currentFileIdx.value = 0
  }
}

function statusTagType(st: string): 'info' | 'primary' | 'success' | 'warning' | 'danger' {
  switch (st) {
    case 'completed':
      return 'success'
    case 'failed':
      return 'danger'
    case 'running':
      return 'primary'
    case 'partial':
      return 'warning'
    case 'queued':
      return 'warning'
    default:
      return 'info'
  }
}

const STATUS_TEXT: Record<string, string> = {
  queued: '排队中',
  running: '解析中',
  completed: '已完成',
  partial: '部分完成',
  failed: '失败',
  canceled: '已取消',
}

function statusText(st: string): string {
  return STATUS_TEXT[st] || st
}

function jobDuration(j: QueueJob): string {
  const ms = (j.files || []).reduce((acc, f) => acc + (f.parse?.duration_ms || 0), 0)
  return ms ? `${(ms / 1000).toFixed(1)}s` : '—'
}

function progressPct(j: QueueJob): number {
  const p = j.progress
  if (!p || !p.total) return j.status === 'completed' ? 100 : 0
  return Math.round(((p.completed + p.failed) / p.total) * 100)
}

function fmtTime(iso: string): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

function shortId(id: string): string {
  return id && id.length > 26 ? `${id.slice(0, 26)}…` : id
}

// ============================================================
// 结果视图（Markdown / JSON 双视图，按文件切换）
// ============================================================
const currentJobId = ref<string | null>(null)
const currentFileIdx = ref(0)
const currentView = ref<'md' | 'json'>('md')
const resultLoading = ref(false)

const currentJob = computed<QueueJob | null>(() =>
  currentJobId.value ? jobs.value.get(currentJobId.value) || null : null,
)
const currentFiles = computed<JobFileResult[]>(() => currentJob.value?.files || [])
const currentFile = computed<JobFileResult | null>(() => currentFiles.value[currentFileIdx.value] || null)

const resultMetaText = computed(() => {
  const j = currentJob.value
  if (!j) return '未选择任务'
  const parts: string[] = [shortId(j.job_id)]
  if (j.tier) parts.push(`tier: ${j.tier}`)
  parts.push(`${j.file_count} 个文件`)
  if (currentFile.value?.parse?.parser_version) parts.push(`v${currentFile.value.parse.parser_version}`)
  return parts.join(' · ')
})

/** 懒加载某个文件的 markdown / structured_json 内容并缓存 */
async function ensureContents(j: QueueJob, fileIdx: number) {
  const f = j.files?.[fileIdx]
  const key = `${j.job_id}:${fileIdx}`
  if (!f || j.contents[key]) return
  const mdRef = f.output_files?.markdown
  const jsonRef = f.output_files?.structured_json
  const entry: { md?: string; json?: string } = {}
  const tasks: Promise<void>[] = []
  if (mdRef?.file_id) {
    tasks.push(
      api<string>(`/v1/files/${encodeURIComponent(mdRef.file_id)}/content`).then(
        (t) => {
          entry.md = typeof t === 'string' ? t : JSON.stringify(t)
        },
        () => {
          entry.md = undefined
        },
      ),
    )
  }
  if (jsonRef?.file_id) {
    tasks.push(
      api<string>(`/v1/files/${encodeURIComponent(jsonRef.file_id)}/content`).then(
        (t) => {
          entry.json = typeof t === 'string' ? t : JSON.stringify(t)
        },
        () => {
          entry.json = undefined
        },
      ),
    )
  }
  await Promise.all(tasks)
  j.contents[key] = entry
  jobs.value = new Map(jobs.value)
}

async function openResult(id: string) {
  const j = jobs.value.get(id)
  if (!j) return
  activeInnerTab.value = 'test' // 结果区在解析测试页签，点查看自动切回
  currentJobId.value = id
  currentFileIdx.value = 0
  // 列表合并的任务没有 files 详情，先拉一次详情
  if (!j.files?.length) {
    resultLoading.value = true
    try {
      const d = await api<JobAsyncResponse>(`/v1/parse/jobs/${encodeURIComponent(id)}`)
      Object.assign(j, { status: d.status, files: d.files, progress: d.progress, tier: d.tier })
      jobs.value = new Map(jobs.value)
    } catch (e) {
      setBanner('error', `加载任务详情失败：${errText(e)}`)
      resultLoading.value = false
      return
    }
  }
  if (!isTerminal(j.status)) {
    setBanner('info', `任务尚在${statusText(j.status)}，结果暂不可查看`)
    resultLoading.value = false
    return
  }
  resultLoading.value = true
  try {
    await ensureContents(j, currentFileIdx.value)
  } finally {
    resultLoading.value = false
  }
}

async function switchFile(val: string | number | boolean | undefined) {
  const idx = Number(val)
  if (!Number.isInteger(idx) || idx < 0) return
  currentFileIdx.value = idx
  const j = currentJob.value
  if (!j) return
  resultLoading.value = true
  try {
    await ensureContents(j, idx)
  } finally {
    resultLoading.value = false
  }
}

const currentContent = computed<{ md?: string; json?: string } | null>(() => {
  const j = currentJob.value
  if (!j) return null
  return j.contents[`${j.job_id}:${currentFileIdx.value}`] || null
})

const mdText = computed(() => currentContent.value?.md || '')
const jsonPretty = computed(() => {
  const raw = currentContent.value?.json
  if (!raw) return ''
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
})

/** markdown 渲染：
 *  - base64 内联图保留，由 CSS 约束为缩略尺寸；
 *  - 云端 SaaS：zip 内相对路径图片（images/xxx.jpg）构造 file_id `batch::idx::路径`
 *    经 GET /v1/files/{id}/content 拉取 blob 渲染，宽度占满内容区；
 *  - 本地 kit：无成员寻址能力，维持占位提示。 */
const imgBlobs = reactive<Record<string, string>>({}) // key `${jobId}:${idx}:${path}` → objectURL
const imgFailed = reactive<Record<string, boolean>>({})
const imgPending = new Set<string>()

function imgKey(jobId: string, idx: number, path: string) {
  return `${jobId}:${idx}:${path}`
}

async function loadJobImage(jobId: string, idx: number, path: string) {
  const key = imgKey(jobId, idx, path)
  if (imgBlobs[key] || imgFailed[key] || imgPending.has(key)) return
  imgPending.add(key)
  try {
    const fileId = `${jobId}::${idx}::${path}`
    const blob = await api<Blob>(`/v1/files/${encodeURIComponent(fileId)}/content`, {
      responseType: 'blob',
    })
    imgBlobs[key] = URL.createObjectURL(blob)
  } catch {
    imgFailed[key] = true
  } finally {
    imgPending.delete(key)
  }
}

const mdHtml = computed(() => {
  if (!mdText.value) return ''
  const job = currentJob.value
  const idx = currentFileIdx.value
  const cloud = config.engine === 'cloud' && !!job
  const pre = mdText.value.replace(
    /!\[([^\]]*)\]\(([^)\s]+)\)/g,
    (m, cap: string, path: string) => {
      if (/^data:image\//i.test(path)) return m // base64 内联图：保留渲染，CSS 约束展示尺寸
      if (!cloud || !job) return `> 🖼️ 图片引用：${cap || path}（图片文件包含在结果 zip 内，未随文本返回）`
      const key = imgKey(job.job_id, idx, path)
      const alt = cap || path.split('/').pop() || path
      if (imgBlobs[key]) return `![${alt}](${imgBlobs[key]})`
      if (imgFailed[key]) return `> ⚠️ 图片加载失败：${path}`
      return `![${alt}](mineru-pending:${key})` // 渲染后由 watcher 替换为加载占位并触发拉取
    },
  )
  let html = marked.parse(pre, { async: false }) as string
  html = html.replace(
    /<img src="mineru-pending:([^"]*)"([^>]*)>/g,
    (_m, key: string, attrs: string) =>
      `<div class="md-img-pending" data-imgkey="${key}"${attrs}>图片加载中…</div>`,
  )
  return html
})

// 渲染完成后扫描加载占位，触发图片懒拉取（blob 就绪 → mdHtml 重算 → <img> 出现）
watch(
  mdHtml,
  (html) => {
    if (!html || config.engine !== 'cloud' || !currentJob.value) return
    const re = /data-imgkey="([^"]+)"/g
    let m: RegExpExecArray | null
    while ((m = re.exec(html))) {
      const parts = m[1].split(':')
      const path = parts.slice(2).join(':')
      if (parts.length >= 3 && path) loadJobImage(parts[0], Number(parts[1]), path)
    }
  },
  { immediate: true },
)

function escapeHtml(src: string): string {
  return src
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function highlightJSON(src: string): string {
  return escapeHtml(src).replace(
    /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(?:\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      let cls = 'j-num'
      if (/^&quot;|^"/.test(match)) {
        cls = /:\s*$/.test(match) ? 'j-key' : 'j-str'
      } else if (/true|false/.test(match)) {
        cls = 'j-bool'
      } else if (/null/.test(match)) {
        cls = 'j-null'
      }
      return `<span class="${cls}">${match}</span>`
    },
  )
}
const highlightedJson = computed(() => (jsonPretty.value ? highlightJSON(jsonPretty.value) : ''))

const byteStats = computed(() => {
  const f = currentFile.value
  return {
    md: f?.output_files?.markdown?.bytes || 0,
    json: f?.output_files?.structured_json?.bytes || 0,
  }
})

function copyText(text: string, ok = '已复制到剪贴板') {
  if (!text) return
  navigator.clipboard.writeText(text).then(
    () => ElMessage.success(ok),
    (e: unknown) => ElMessage.error(`复制失败：${errText(e)}`),
  )
}

function handleCopyResult() {
  if (!currentContent.value) {
    ElMessage.warning('请先在队列中查看一条已完成任务')
    return
  }
  if (currentView.value === 'md') {
    copyText(mdText.value || '（无 markdown 内容）')
  } else {
    copyText(jsonPretty.value || '（无 JSON 内容）')
  }
}

function downloadBlob(content: string | Blob, filename: string, mime: string) {
  const blob =
    typeof content === 'string' ? new Blob([content], { type: `${mime};charset=utf-8` }) : content
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function baseName(): string {
  const n = currentFile.value?.name || currentJob.value?.job_id || 'result'
  return n.replace(/\.[^.]+$/, '').replace(/[^\w\u4e00-\u9fa5.-]+/g, '_')
}

const downloadingZip = ref(false)

async function handleDownloadResult() {
  if (!currentContent.value) {
    ElMessage.warning('请先在队列中查看一条已完成任务')
    return
  }
  if (currentView.value === 'md') {
    if (!mdText.value) {
      ElMessage.warning('当前文件无 markdown 内容')
      return
    }
    // 云端模式：markdown 引用 zip 内相对路径图片，打包下载原始结果 zip（解压即为完整内容）；
    // 本地 kit 的 markdown 图片为 base64 内联，直接下载 .md 即自包含
    const job = currentJob.value
    if (config.engine === 'cloud' && job) {
      downloadingZip.value = true
      try {
        const blob = await api<Blob>(
          `/v1/parse/jobs/${encodeURIComponent(job.job_id)}/files/${currentFileIdx.value}/result-zip`,
          { responseType: 'blob' },
        )
        downloadBlob(blob, `${baseName()}_markdown.zip`, 'application/zip')
      } catch (e) {
        ElMessage.error(`结果包下载失败：${errText(e)}`)
      } finally {
        downloadingZip.value = false
      }
      return
    }
    downloadBlob(mdText.value, `${baseName()}.md`, 'text/markdown')
  } else {
    if (!jsonPretty.value) {
      ElMessage.warning('当前文件无 JSON 内容')
      return
    }
    downloadBlob(jsonPretty.value, `${baseName()}_structured.json`, 'application/json')
  }
}

// ============================================================
// 生命周期
// ============================================================
onMounted(() => {
  loadConfig()
  void runHealthCheck(true)
  void loadTiers()
  void syncServerJobs(true)
})

onBeforeUnmount(() => {
  stopAllPolls()
  saveConfig()
})
</script>

<template>
  <div class="parser-page">
    <!-- 顶部：连接配置 -->
    <el-card shadow="never" class="conn-card">
      <div class="conn-row">
        <div class="conn-fields">
          <!-- 引擎切换：本地 mineru-kit / 云端 SaaS（mineru.net，Key 走模型配置页） -->
          <!-- 说明 ? 角标挂在对象右上角（不占行内主轴位置），hover 展示说明 -->
          <div class="engine-wrap">
            <el-radio-group v-model="config.engine" size="default" @change="handleEngineChange">
              <el-radio-button value="local">本地服务</el-radio-button>
              <el-radio-button value="cloud">云端 SaaS</el-radio-button>
            </el-radio-group>
            <el-tooltip placement="top" :content="engineHint">
              <el-icon class="conn-info"><QuestionFilled /></el-icon>
            </el-tooltip>
          </div>
          <!-- 双模式等宽占位：本地=可编辑 Base URL；云端=只读 SaaS 端点，切换不跳动 -->
          <el-input
            v-if="config.engine === 'local'"
            v-model="config.baseUrl"
            placeholder="MinerU 服务地址"
            style="width: 280px"
          >
            <template #prepend>Base</template>
          </el-input>
          <el-input v-else model-value="https://mineru.net/api/v4" disabled style="width: 280px">
            <template #prepend>Base</template>
          </el-input>
          <!-- 连接测试：右上角圆点轻提示连通状态（绿=正常，红=失败），hover 展示详情 -->
          <div class="test-btn-wrap">
            <el-button type="primary" plain :icon="Connection" :loading="health.checking" @click="handleTestConn">
              连接测试
            </el-button>
            <el-tooltip v-if="dotClass" placement="top" :content="dotTip">
              <span class="conn-dot" :class="dotClass" />
            </el-tooltip>
          </div>
        </div>
      </div>
    </el-card>

    <el-tabs v-model="activeInnerTab" class="inner-tabs">
      <el-tab-pane label="解析测试" name="test">
    <el-row :gutter="16" class="main-row">
      <!-- 左列：上传 + 参数 -->
      <el-col :xs="24" :sm="24" :md="10" :lg="9" :xl="8">
        <el-card shadow="never" class="section-card">
          <template #header>
            <div class="card-header">
              <span>文件上传</span>
              <span class="hint">PDF / PNG / JPG · 单文件 ≤ {{ MAX_FILE_MB }} MB · 可多选</span>
            </div>
          </template>

          <el-upload
            ref="uploadRef"
            drag
            multiple
            :auto-upload="false"
            :show-file-list="false"
            :accept="ACCEPT_EXT"
            :on-change="handleFilePick"
            class="dropzone"
            :class="{ 'is-hover': dropHover }"
          >
            <el-icon class="drop-icon"><UploadFilled /></el-icon>
            <div class="drop-text">拖拽文件到此处，或 <em>点击选择</em></div>
            <div class="drop-hint">选中的文件将合并提交为一个解析任务</div>
          </el-upload>

          <div v-if="selectedFiles.length" class="file-list">
            <div v-for="(f, i) in selectedFiles" :key="i" class="file-info">
              <span class="file-name" :title="f.name">{{ f.name }}</span>
              <span class="muted small">{{ fmtSize(f.size) }}</span>
              <el-button link type="danger" size="small" :icon="Delete" @click="removeFile(i)">
                移除
              </el-button>
            </div>
          </div>

          <el-form label-position="top" class="opts-form">
            <el-form-item label="解析档位 tier（留空 = 服务端默认）">
              <el-select
                v-model="options.tier"
                placeholder="服务端默认"
                clearable
                :loading="tiersLoading"
                style="width: 100%"
              >
                <el-option
                  v-for="t in tiers.length ? tiers : TIER_FALLBACK"
                  :key="t.id"
                  :label="t.id"
                  :value="t.id"
                >
                  <span class="opt-label">{{ t.id }}</span>
                  <span class="opt-desc">{{ t.description }}<template v-if="t.current_model"> · {{ t.current_model }}</template></span>
                </el-option>
              </el-select>
            </el-form-item>
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="OCR 模式">
                  <el-select v-model="options.ocrMode" style="width: 100%">
                    <el-option v-for="o in OCR_OPTIONS" :key="o.value" :label="o.label" :value="o.value">
                      <span class="opt-label">{{ o.label }}</span>
                      <span class="opt-desc">{{ o.desc }}</span>
                    </el-option>
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="页码范围（可选）">
                  <el-input v-model="options.pageRange" placeholder="如 1-5,8" />
                </el-form-item>
              </el-col>
            </el-row>
            <div class="hint">
              输出格式固定为 Markdown + 结构化 JSON（structured_json）；页码范围仅对首个 PDF 生效。
            </div>
          </el-form>

          <el-button
            type="primary"
            :icon="Upload"
            :disabled="!selectedFiles.length"
            :loading="uploading"
            class="submit-btn"
            @click="submitParse"
          >
            {{ uploading ? '上传并提交中…' : `提交解析${selectedFiles.length ? `（${selectedFiles.length} 个文件）` : ''}` }}
          </el-button>
        </el-card>
      </el-col>

      <!-- 右列：结果视图（Markdown / JSON） -->
      <el-col :xs="24" :sm="24" :md="14" :lg="15" :xl="16">
        <el-card shadow="never" class="section-card result-card">
          <template #header>
            <div class="card-header">
              <span>解析结果</span>
              <span class="muted small">{{ resultMetaText }}</span>
            </div>
          </template>

          <div v-if="currentFiles.length > 1" class="file-switch">
            <el-radio-group :model-value="currentFileIdx" size="small" @change="switchFile">
              <el-radio-button
                v-for="(f, i) in currentFiles"
                :key="i"
                :value="i"
                :aria-label="`查看文件 ${f.name}`"
              >
                {{ f.name }}
              </el-radio-button>
            </el-radio-group>
          </div>

          <el-tabs v-model="currentView" class="result-tabs">
            <el-tab-pane label="Markdown" name="md" />
            <el-tab-pane label="JSON" name="json" />
          </el-tabs>

          <div class="result-toolbar">
            <el-button size="small" plain :icon="DocumentCopy" :disabled="!currentContent" @click="handleCopyResult">
              复制
            </el-button>
            <el-button
              size="small"
              plain
              :icon="Download"
              :disabled="!currentContent"
              :loading="downloadingZip"
              :title="config.engine === 'cloud' ? '云端模式：下载结果 zip（markdown + images 文件夹），解压后即为完整内容' : '下载当前视图文件'"
              @click="handleDownloadResult"
            >
              下载当前视图
            </el-button>
            <span class="muted small toolbar-hint">
              <template v-if="!currentJob">在队列中点击「查看」以加载结果</template>
              <template v-else-if="resultLoading">正在加载结果…</template>
              <template v-else-if="!currentContent">当前文件暂无结果</template>
              <template v-else>
                Markdown {{ (byteStats.md / 1024).toFixed(1) }} KB
                · JSON {{ (byteStats.json / 1024).toFixed(1) }} KB
                <template v-if="currentFile?.parse?.duration_ms">
                  · 解析耗时 {{ (currentFile.parse.duration_ms / 1000).toFixed(1) }}s
                </template>
              </template>
            </span>
          </div>

          <div class="result-body" v-loading="resultLoading">
            <el-empty
              v-if="!currentJob || !currentContent"
              description="在队列中点击「查看」以加载结果"
              :image-size="80"
            />
            <!-- eslint-disable-next-line vue/no-v-html -->
            <div v-else-if="currentView === 'md'" class="md-view" v-html="mdHtml" />
            <!-- eslint-disable-next-line vue/no-v-html -->
            <pre v-else class="json-view" v-html="highlightedJson" />
          </div>
        </el-card>
      </el-col>
    </el-row>
      </el-tab-pane>

      <!-- 页签二：解析队列（筛选 + 全量任务表） -->
      <el-tab-pane label="解析队列" name="queue">
        <div class="queue-toolbar">
          <el-select v-model="queueFilter.status" clearable placeholder="全部状态" style="width: 132px">
            <el-option v-for="(text, st) in STATUS_TEXT" :key="st" :label="text" :value="st" />
          </el-select>
          <el-input
            v-model="queueFilter.keyword"
            clearable
            placeholder="按文档名称 / 任务 ID 筛选"
            :prefix-icon="Search"
            style="width: 240px"
          />
          <el-tag size="small" type="info" effect="plain">
            共 {{ queueStats.total }} · 进行 {{ queueStats.running }} · 完成 {{ queueStats.completed }} · 失败 {{ queueStats.failed }}
          </el-tag>
          <span class="muted small">{{ filteredQueue.length }} / {{ jobRows.length }} 条</span>
          <span class="toolbar-spacer" />
          <el-button link type="primary" size="small" :icon="Refresh" @click="syncServerJobs(false)">
            从服务端拉取
          </el-button>
        </div>

        <el-table
          :data="filteredQueue"
          size="small"
          :empty-text="jobRows.length ? '无匹配任务：调整筛选条件' : '队列为空：到「解析测试」上传文件提交任务'"
          max-height="520"
        >
          <el-table-column label="任务" width="225">
            <template #default="{ row }">
              <div class="mono small">{{ shortId(row.job_id) }}</div>
              <div class="muted small">{{ fmtTime(row.created_at) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="文档名称" min-width="200">
            <template #default="{ row }">
              <span
                v-if="jobFileNames(row as QueueJob)"
                class="small doc-name"
                :title="jobFileNames(row as QueueJob)"
              >{{ jobFileNames(row as QueueJob) }}</span>
              <span v-else class="muted small">{{ row.file_count }} 个文件（详情未加载）</span>
            </template>
          </el-table-column>
          <el-table-column label="产物ID" width="165">
            <template #default="{ row }">
              <template v-if="jobFileIds(row as QueueJob).length">
                <div
                  v-for="f in jobFileIds(row as QueueJob)"
                  :key="f.id"
                  class="file-id-row"
                  :title="`${f.name}\nGET /v1/files/${f.id}/content（markdown 产物）`"
                >
                  <span class="mono small">{{ shortId(f.id) }}</span>
                  <el-button
                    link
                    size="small"
                    :icon="DocumentCopy"
                    :aria-label="`复制 ${f.name} 的产物文件ID`"
                    @click="copyText(f.id, '产物文件 ID 已复制：GET /v1/files/{id}/content 可下载')"
                  />
                </div>
              </template>
              <span v-else class="muted small">—</span>
            </template>
          </el-table-column>
          <el-table-column label="类型" width="90" align="center">
            <template #default="{ row }">
              <template v-if="jobFileTypes(row as QueueJob).length">
                <el-tag
                  v-for="t in jobFileTypes(row as QueueJob)"
                  :key="t"
                  size="small"
                  effect="plain"
                  class="type-tag"
                >{{ t }}</el-tag>
              </template>
              <span v-else class="muted small">—</span>
            </template>
          </el-table-column>
          <el-table-column label="文件数" width="80" align="center">
            <template #default="{ row }">
              <span class="small">{{ row.file_count }}</span>
            </template>
          </el-table-column>
          <el-table-column label="档位" width="100">
            <template #default="{ row }">
              <span class="small muted">{{ row.tier || '默认' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag :type="statusTagType(row.status)" size="small" effect="light">
                {{ statusText(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="进度" width="160">
            <template #default="{ row }">
              <el-progress
                :percentage="progressPct(row as QueueJob)"
                :stroke-width="8"
                :status="row.status === 'failed' || row.status === 'canceled' ? 'exception' : row.status === 'completed' ? 'success' : undefined"
              />
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="90">
            <template #default="{ row }">
              <span class="small muted">{{ isTerminal(row.status) ? jobDuration(row as QueueJob) : '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="170" fixed="right">
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                size="small"
                :disabled="!isTerminal(row.status)"
                @click="openResult(row.job_id)"
              >查看</el-button>
              <el-button
                v-if="row.status === 'queued' || row.status === 'running'"
                link
                type="warning"
                size="small"
                @click="handleCancelJob(row.job_id)"
              >取消</el-button>
              <el-button link type="danger" size="small" @click="handleRemoveJob(row.job_id)">移除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.parser-page {
  padding: 12px 16px 24px;
  min-height: 100%;
  background: #f5f7fa;
}

/* ===== 顶部连接卡 ===== */
.conn-card { margin-bottom: 14px; }
.conn-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 16px;
}
.conn-fields {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
/* 连接测试按钮容器：状态圆点角标绝对定位于右上角，不占行内主轴位置 */
.test-btn-wrap {
  position: relative;
  display: inline-flex;
}
.conn-dot {
  position: absolute;
  top: -5px;
  right: -5px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 2px solid #fff;
  box-sizing: content-box;
  z-index: 1;
}
.conn-dot.is-success {
  background: var(--el-color-success);
}
.conn-dot.is-danger {
  background: var(--el-color-danger);
}
/* 引擎切换容器：? 角标绝对定位于右上角，不占据行内主轴位置 */
.engine-wrap {
  position: relative;
  display: inline-flex;
}
.conn-info {
  position: absolute;
  top: -7px;
  right: -9px;
  z-index: 1;
  color: #909399;
  font-size: 14px;
  cursor: help;
}
.conn-info:hover {
  color: var(--el-color-primary);
}

/* ===== 通用区块 ===== */
.main-row { margin-bottom: 0; }

/* 内部页签：解析测试 / 解析队列 */
.inner-tabs :deep(.el-tabs__header) { margin-bottom: 14px; }

/* 队列页签工具行：筛选 + 统计 + 拉取 */
.queue-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.queue-toolbar .toolbar-spacer { flex: 1; }
.type-tag + .type-tag { margin-left: 4px; }
.doc-name { color: #2c3e50; }

/* 文件ID 行：截断 ID + 复制按钮 同行 */
.file-id-row {
  display: flex;
  align-items: center;
  gap: 2px;
}
.file-id-row .mono {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.section-card {
  margin-bottom: 14px;
  border-radius: 10px;
}
.section-card :deep(.el-card__header) {
  padding: 10px 14px;
  background: #eaf2fb;
  border-bottom: 1px solid #d7e1ec;
}
.section-card :deep(.el-card__body) { padding: 14px; }
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 14px;
  font-weight: 600;
  color: #1f3864;
  flex-wrap: wrap;
}
.card-header .header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 400;
}
.hint { color: #909399; font-size: 12px; line-height: 1.7; font-weight: 400; }
.small { font-size: 12px; }
.muted { color: #909399; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }

/* ===== 上传区 ===== */
.dropzone :deep(.el-upload-dragger) {
  background: #eaf2fb;
  border: 2px dashed #4a90d9;
  border-radius: 8px;
  padding: 22px 12px;
  transition: 0.15s;
}
.dropzone :deep(.el-upload-dragger:hover),
.dropzone.is-hover :deep(.el-upload-dragger) {
  background: #dceaf8;
  border-color: #2f6da8;
}
.drop-icon {
  font-size: 42px;
  color: #4a90d9;
  margin-bottom: 6px;
}
.drop-text {
  color: #5a6b7b;
  font-size: 13px;
}
.drop-text em {
  color: #1f3864;
  font-weight: 600;
  font-style: normal;
}
.drop-hint {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
}
.file-list {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.file-info {
  padding: 6px 10px;
  background: #f0f7ff;
  border: 1px solid #d7e1ec;
  border-radius: 6px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
}
.file-info .file-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ===== 参数与提交 ===== */
.opts-form { margin-top: 14px; }
.opt-label { font-weight: 600; }
.opt-desc {
  float: right;
  color: #909399;
  font-size: 12px;
}
.submit-btn { width: 100%; margin-top: 4px; }

/* ===== 结果区 ===== */
.result-card { display: flex; flex-direction: column; }
.result-card :deep(.el-card__body) {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 420px;
}
.file-switch { margin-bottom: 10px; }
.result-tabs :deep(.el-tabs__header) { margin-bottom: 8px; }
.result-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.toolbar-hint { margin-left: auto; }
.result-body {
  flex: 1;
  overflow: auto;
  border: 1px solid #e5e8ee;
  border-radius: 8px;
  background: #fff;
  padding: 14px;
  min-height: 320px;
  max-height: 60vh; /* 上限约束：内容超出时在窗口内滚动，不撑开页面 */
}

/* Markdown 视图 */
.md-view {
  font-size: 14px;
  line-height: 1.8;
  color: #2c3e50;
  word-break: break-word;
}
/* base64 内联图：约束为缩略尺寸，避免大图挤满视图 */
.md-view :deep(img) {
  display: block;
  margin: 8px auto;
  max-width: 100%;
  max-height: 220px;
  object-fit: contain;
  border: 1px solid #e5e8ee;
  border-radius: 6px;
  background: #fafbfc;
}
/* zip 内图片（blob: 懒加载产物）：占据完整内容宽度 */
.md-view :deep(img[src^='blob:']) {
  width: 100%;
  max-height: none;
  margin: 10px 0;
  object-fit: contain;
}
/* 图片懒加载占位 */
.md-view :deep(.md-img-pending) {
  margin: 10px 0;
  padding: 18px 12px;
  text-align: center;
  color: #909399;
  font-size: 13px;
  background: #fafbfc;
  border: 1px dashed #dcdfe6;
  border-radius: 6px;
}
.md-view :deep(h1),
.md-view :deep(h2),
.md-view :deep(h3),
.md-view :deep(h4) {
  color: #1f3864;
  margin: 18px 0 8px;
  font-weight: 600;
}
.md-view :deep(h1) { font-size: 20px; }
.md-view :deep(h2) { font-size: 17px; }
.md-view :deep(h3) { font-size: 15px; }
.md-view :deep(p) { margin: 8px 0; }
.md-view :deep(blockquote) {
  margin: 8px 0;
  padding: 6px 12px;
  border-left: 3px solid #4a90d9;
  background: #f0f7ff;
  border-radius: 0 6px 6px 0;
  color: #5a6b7b;
  font-size: 13px;
}
.md-view :deep(table) {
  border-collapse: collapse;
  margin: 10px 0;
  width: 100%;
  font-size: 13px;
}
.md-view :deep(th),
.md-view :deep(td) {
  border: 1px solid #d7e1ec;
  padding: 6px 10px;
  text-align: left;
}
.md-view :deep(th) { background: #eaf2fb; color: #1f3864; }
.md-view :deep(code) {
  background: #f0f4f8;
  border-radius: 4px;
  padding: 1px 5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
}
.md-view :deep(pre) {
  background: #f6f8fa;
  border: 1px solid #e5e8ee;
  border-radius: 6px;
  padding: 10px 12px;
  overflow: auto;
}
.md-view :deep(pre code) { background: none; padding: 0; }

/* JSON 视图 */
.json-view {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
  color: #2c3e50;
}
.json-view :deep(.j-key) { color: #1f3864; font-weight: 600; }
.json-view :deep(.j-str) { color: #2f6da8; }
.json-view :deep(.j-num) { color: #b75000; }
.json-view :deep(.j-bool) { color: #8f5b0a; }
.json-view :deep(.j-null) { color: #909399; }

/* ===== 队列表格 ===== */
</style>
