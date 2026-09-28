<template>
  <div class="kge-page qa-page">
    <!-- 过滤栏 -->
    <div class="filter-bar">
      <div class="filter-item">
        <span class="filter-label">检索时间</span>
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          range-separator="–"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          value-format="YYYY-MM-DD"
          style="width: 260px"
        />
      </div>
      <div class="filter-item">
        <span class="filter-label">检索内容</span>
        <el-input v-model="filters.keyword" placeholder="请输入问题或回答关键词" clearable style="width: 220px" @keyup.enter="onSearch" />
      </div>
      <div class="filter-item">
        <span class="filter-label">用户</span>
        <el-input v-model="filters.user" placeholder="请输入用户名" clearable style="width: 160px" @keyup.enter="onSearch" />
      </div>
      <div class="filter-item">
        <span class="filter-label">反馈</span>
        <el-select v-model="filters.feedback" placeholder="全部反馈" clearable style="width: 140px">
          <el-option label="👍 有帮助" value="helpful" />
          <el-option label="👎 纠错" value="correct" />
          <el-option label="❓ 没找到" value="notfound" />
          <el-option label="无反馈" value="none" />
        </el-select>
      </div>
      <el-button type="primary" @click="onSearch">查询</el-button>
      <el-button @click="onReset">重置</el-button>
    </div>

    <!-- 块状列表 -->
    <div v-loading="loading" class="qa-list">
      <div v-if="!loading && !items.length" class="empty-box">
        <el-icon :size="40" color="#C0C4CC"><Document /></el-icon>
        <div class="empty-text">暂无问答数据</div>
      </div>

      <div v-for="item in items" :key="item.question_id" class="qa-block">
        <!-- 用户信息行 -->
        <div class="qa-user-row">
          <div class="avatar">{{ (item.username || '?').charAt(0).toUpperCase() }}</div>
          <div class="user-meta">
            <span class="user-name">{{ item.username }}</span>
            <span class="asked-at">{{ item.asked_at }}</span>
          </div>
          <div class="fb-tags">
            <el-tag v-for="(fb, i) in item.feedbacks" :key="i" size="small"
                    :type="fb.feedback_type === 'helpful' ? 'success' : fb.feedback_type === 'correct' ? 'danger' : 'info'">
              {{ fbLabel(fb.feedback_type) }}{{ fb.error_type ? `·${fb.error_type}` : '' }}
            </el-tag>
          </div>
        </div>

        <!-- 元信息行 -->
        <div class="qa-meta-row">
          <span class="meta-chip">系统编码：{{ item.model || item.meta || 'DeerFlow' }}</span>
          <el-divider direction="vertical" />
          <span class="meta-chip">TopK：{{ item.retrieval.length || '-' }}</span>
          <el-divider direction="vertical" />
          <template v-if="item.duration_ms">
            <span class="meta-chip">耗时：{{ (item.duration_ms / 1000).toFixed(2) }}s</span>
            <el-divider direction="vertical" />
          </template>
          <span class="meta-chip link" @click="toggleChain(item)">
            链路ID：{{ shortId(item.message_id) }} <el-icon><View /></el-icon>
          </span>
        </div>

        <!-- 回答链路（展开/收起） -->
        <el-collapse-transition>
          <div v-if="expanded.has(item.question_id)" class="chain-box">
            <div class="chain-title">回答链路</div>
            <div v-if="!item.steps.length" class="chain-empty">该轮问答未记录链路步骤</div>
            <div v-for="(st, si) in item.steps" :key="si" class="chain-step">
              <span class="step-dot" :class="{ done: st.done !== false }">{{ si + 1 }}</span>
              <span class="step-title">{{ st.title }}</span>
              <span v-if="st.detail" class="step-detail">{{ st.detail }}</span>
            </div>
          </div>
        </el-collapse-transition>

        <!-- 问题（点击展开/收起回答） -->
        <div class="qa-question" @click="toggleAnswer(item)">
          <span class="q-tag">Q</span>
          <span class="q-text">{{ item.question }}</span>
          <span class="q-hint">{{ answerExpanded.has(item.question_id) ? '收起' : '点击查看回答' }}</span>
          <el-icon class="q-arrow" :class="{ open: answerExpanded.has(item.question_id) }"><ArrowDown /></el-icon>
        </div>

        <!-- 回答 + 召回结果（默认收起） -->
        <el-collapse-transition>
          <div v-show="answerExpanded.has(item.question_id)" class="retrieval-box">
            <div class="retrieval-head">
              <span class="retrieval-title">召回结果（{{ item.retrieval.length }} 条，其中被答案引用 {{ citedCount(item) }} 条）</span>
            </div>
            <div v-if="item.answer" class="answer-box" v-html="renderAnswer(item.answer)"></div>

          <div v-if="!item.retrieval.length" class="no-retrieval">本轮未召回到知识库片段（智能体直答或未命中）</div>
          <div
            v-for="(hit, hi) in visibleRetrieval(item)"
            :key="hi"
            class="hit-card"
          >
            <div class="hit-top">
              <span class="top-badge" :class="{ top3: hi < 3 }">TOP {{ String(hi + 1).padStart(2, '0') }}</span>
              <el-tag v-if="hit.cited === false" size="small" type="info" effect="plain" class="cite-tag">未引用</el-tag>
              <el-tag v-else size="small" type="success" effect="plain" class="cite-tag">已引用</el-tag>
              <div class="score-area">
                <span class="score-label">Score</span>
                <div class="score-bar">
                  <div class="score-fill" :style="{ width: scoreWidth(hit.score) }"></div>
                </div>
                <span class="score-val">{{ hit.score != null ? Number(hit.score).toFixed(4) : '-' }}</span>
              </div>
              <a v-if="hit.url" class="hit-title hit-link" :href="hit.url" target="_blank" rel="noopener">
                <el-icon><Document /></el-icon>
                {{ hit.document_title || hit.title || '未知文档' }}
              </a>
              <span v-else class="hit-title">
                <el-icon><Document /></el-icon>
                {{ hit.document_title || hit.title || '未知文档' }}
              </span>
            </div>
            <div class="hit-snippet">{{ hit.text }}</div>
            <div class="hit-foot">
              <span v-if="hit.chunk_id">片段ID：{{ shortId(hit.chunk_id) }}</span>
              <span v-if="hit.chunk_index != null">分段：{{ hit.chunk_index }}</span>
              <span v-if="hit.page_number != null">页码：{{ hit.page_number }}</span>
            </div>
          </div>
          <el-button v-if="item.retrieval.length > 3 && !retrievalExpanded.has(item.question_id)" link type="primary" size="small" @click="retrievalExpanded.add(item.question_id)">
            展开全部 {{ item.retrieval.length }} 条召回
          </el-button>
          </div>
        </el-collapse-transition>
      </div>
    </div>

    <!-- 分页 -->
    <div v-if="total > 0" class="pager">
      <span class="pager-total">共 {{ total }} 条</span>
      <el-select v-model="pageSize" size="small" style="width: 100px" @change="onSearch">
        <el-option :value="20" label="20条/页" />
        <el-option :value="50" label="50条/页" />
      </el-select>
      <el-pagination
        background
        layout="prev, pager, next"
        :total="total"
        :page-size="pageSize"
        :current-page="page"
        @current-change="onPageChange"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { Document, View, ArrowDown } from '@element-plus/icons-vue'
import { getQaDetails, type QaDetailItem } from '@/api/qa'

const loading = ref(false)
const items = ref<QaDetailItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

const dateRange = ref<[string, string] | null>(null)
const filters = reactive({ keyword: '', user: '', feedback: '' })

// 展开状态：链路 / 回答 / 全部召回
const expanded = reactive(new Set<string>())
const answerExpanded = reactive(new Set<string>())
const retrievalExpanded = reactive(new Set<string>())

function toggleChain(item: QaDetailItem) {
  expanded.has(item.question_id) ? expanded.delete(item.question_id) : expanded.add(item.question_id)
}
function toggleAnswer(item: QaDetailItem) {
  answerExpanded.has(item.question_id) ? answerExpanded.delete(item.question_id) : answerExpanded.add(item.question_id)
}
function visibleRetrieval(item: QaDetailItem) {
  return retrievalExpanded.has(item.question_id) ? item.retrieval : item.retrieval.slice(0, 3)
}
function citedCount(item: QaDetailItem) {
  // 旧数据无 cited 字段（仅记录最终引用），视为全部已引用
  return item.retrieval.filter((h) => h.cited !== false).length
}

function fbLabel(t: string) {
  return { helpful: '👍 有帮助', correct: '👎 纠错', notfound: '❓ 没找到' }[t] || t
}
function shortId(id?: string | null) {
  return id ? id.slice(0, 8) + '…' : '-'
}
function scoreWidth(score?: number) {
  if (score == null) return '0%'
  // 相关性分数多在 0~1，做相对展示
  return `${Math.min(100, Math.max(4, Number(score) * 100))}%`
}
function renderAnswer(md: string) {
  // 轻量 markdown：转义 + 代码块/粗体/换行/列表
  const esc = md.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  return esc
    .replace(/```[\s\S]*?```/g, (m) => `<pre class="md-pre">${m.replace(/```/g, '').trim()}</pre>`)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/^###\s*(.+)$/gm, '<h4>$1</h4>')
    .replace(/^##\s*(.+)$/gm, '<h3>$1</h3>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/\n/g, '<br/>')
}

async function load() {
  loading.value = true
  try {
    const res = await getQaDetails({
      date_from: dateRange.value?.[0],
      date_to: dateRange.value?.[1],
      keyword: filters.keyword || undefined,
      user: filters.user || undefined,
      feedback: filters.feedback || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    items.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

function onSearch() {
  page.value = 1
  load()
}
function onReset() {
  dateRange.value = null
  filters.keyword = ''
  filters.user = ''
  filters.feedback = ''
  page.value = 1
  load()
}
function onPageChange(p: number) {
  page.value = p
  load()
}

onMounted(load)
</script>

<style scoped>
/* kge-page 为 flex 纵向容器：过滤栏/分页固定，列表撑满剩余高度并内部滚动（kge-fill 约定） */
.filter-bar {
  display: flex; align-items: center; flex-wrap: wrap; gap: 12px;
  background: #fff; border: 1px solid var(--line, #E5E7EB); border-radius: 10px;
  padding: 14px 16px; margin-bottom: 14px;
  flex-shrink: 0;
}
.filter-item { display: flex; align-items: center; gap: 8px; }
.filter-label { font-size: 13.5px; color: #4B5563; white-space: nowrap; }

.qa-list { display: flex; flex-direction: column; gap: 14px; flex: 1; min-height: 0; overflow-y: auto; }
.empty-box { text-align: center; padding: 60px 0; color: #9CA3AF; }
.empty-text { margin-top: 10px; font-size: 14px; }

.qa-block {
  background: #fff; border: 1px solid var(--line, #E5E7EB); border-radius: 12px;
  padding: 18px 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.03);
}
.qa-user-row { display: flex; align-items: center; gap: 12px; }
.avatar {
  width: 38px; height: 38px; border-radius: 50%; background: #2563EB; color: #fff;
  display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 16px;
  flex-shrink: 0;
}
.user-meta { display: flex; align-items: baseline; gap: 12px; }
.user-name { font-weight: 600; font-size: 15px; color: #1F2937; }
.asked-at { font-size: 13px; color: #9CA3AF; }
.fb-tags { margin-left: auto; display: flex; gap: 6px; }

.qa-meta-row {
  display: flex; align-items: center; flex-wrap: wrap; gap: 4px;
  margin: 12px 0 4px 50px; font-size: 12.5px; color: #6B7280;
}
.meta-chip { color: #6B7280; }
.meta-chip.link { color: #2563EB; cursor: pointer; display: inline-flex; align-items: center; gap: 2px; }
.meta-chip.link:hover { text-decoration: underline; }

.chain-box {
  margin: 8px 0 4px 50px; background: #F9FAFB; border: 1px dashed #E5E7EB;
  border-radius: 8px; padding: 12px 14px;
}
.chain-title { font-size: 13px; font-weight: 600; color: #374151; margin-bottom: 8px; }
.chain-empty { font-size: 12.5px; color: #9CA3AF; }
.chain-step { display: flex; align-items: center; gap: 8px; padding: 3px 0; font-size: 13px; }
.step-dot {
  width: 18px; height: 18px; border-radius: 50%; background: #E5E7EB; color: #6B7280;
  display: inline-flex; align-items: center; justify-content: center; font-size: 11px; flex-shrink: 0;
}
.step-dot.done { background: #DBEAFE; color: #2563EB; }
.step-title { color: #374151; font-weight: 500; }
.step-detail { color: #9CA3AF; font-size: 12.5px; }

.qa-question {
  display: flex; align-items: center; gap: 10px; margin: 12px 0 10px;
  cursor: pointer; user-select: none; padding: 6px 8px; margin-left: -8px;
  border-radius: 8px; transition: background 0.15s;
}
.qa-question:hover { background: #F3F4F6; }
.q-tag { color: #2563EB; font-weight: 800; font-size: 15px; flex-shrink: 0; }
.q-text { color: #2563EB; font-weight: 600; font-size: 15px; line-height: 1.6; flex: 1; min-width: 0; }
.q-hint { font-size: 12.5px; color: #9CA3AF; flex-shrink: 0; white-space: nowrap; }
.q-arrow { color: #9CA3AF; transition: transform 0.2s; flex-shrink: 0; }
.q-arrow.open { transform: rotate(180deg); }

.retrieval-box { margin-left: 26px; }
.retrieval-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.retrieval-title { font-size: 13.5px; color: #6B7280; font-weight: 500; }
.answer-box {
  background: #F0F9FF; border: 1px solid #BAE6FD; border-radius: 8px;
  padding: 12px 14px; font-size: 13.5px; line-height: 1.7; color: #334155; margin-bottom: 10px;
}
.answer-box :deep(.md-pre) {
  background: #fff; border: 1px solid #E5E7EB; border-radius: 6px; padding: 8px;
  font-size: 12.5px; overflow-x: auto; white-space: pre-wrap;
}
.no-retrieval { font-size: 13px; color: #9CA3AF; padding: 8px 0; }

.hit-card {
  background: #FAFBFC; border: 1px solid #EEF0F3; border-radius: 10px;
  padding: 12px 14px; margin-bottom: 8px;
}
.hit-top { display: flex; align-items: center; gap: 12px; }
.cite-tag { flex-shrink: 0; }
.top-badge {
  background: #E5E7EB; color: #6B7280; font-weight: 700; font-size: 12px;
  padding: 4px 8px; border-radius: 6px; flex-shrink: 0;
}
.top-badge.top3 { background: #FFF7ED; color: #EA580C; }
.score-area { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 160px; max-width: 320px; }
.score-label { font-size: 12px; color: #9CA3AF; }
.score-bar { flex: 1; height: 6px; background: #E5E7EB; border-radius: 3px; overflow: hidden; }
.score-fill { height: 100%; background: linear-gradient(90deg, #3B82F6, #2563EB); border-radius: 3px; }
.score-val { font-size: 12.5px; font-weight: 600; color: #374151; min-width: 62px; }
.hit-title {
  font-size: 13.5px; font-weight: 600; color: #1F2937; display: inline-flex;
  align-items: center; gap: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
a.hit-link { text-decoration: none; }
a.hit-link:hover { color: #2563EB; text-decoration: underline; }
.hit-snippet {
  margin-top: 8px; font-size: 13px; line-height: 1.7; color: #4B5563;
  max-height: 110px; overflow: hidden; text-overflow: ellipsis;
  display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical;
}
.hit-foot { margin-top: 8px; display: flex; gap: 18px; font-size: 12px; color: #9CA3AF; }

.pager {
  display: flex; align-items: center; justify-content: flex-end; gap: 14px;
  margin-top: 14px;
  flex-shrink: 0;
}
.pager-total { font-size: 13.5px; color: #6B7280; }
</style>
