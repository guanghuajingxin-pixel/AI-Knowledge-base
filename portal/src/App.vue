<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElAlert, ElButton, ElDialog, ElForm, ElFormItem, ElIcon, ElInput, ElTooltip, vLoading } from 'element-plus'
import { ArrowUp, ArrowUpRight, ExternalLink, LogIn, LogOut, MessageCircle, PanelLeftClose, PanelLeftOpen, Plus, RotateCw, Sparkles, Square } from '@lucide/vue'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { applications, pageFromHash, type PageId } from './config/apps'
import { authConfig, identity as sharedIdentity } from './auth/session'
import { useChat } from './composables/use-chat'
import { lucideIconMap } from './utils/lucide-icons'
import LoginView from './views/LoginView.vue'

const loggedIn = computed(() => Boolean(sharedIdentity.value))
function logoutPortal() { signOut() }
const page = ref<PageId>(pageFromHash(location.hash))
const isCollapsed = ref<boolean>(false)
const frameKey = ref<number>(0)
const isFrameLoading = ref<boolean>(false)
const isFrameSlow = ref<boolean>(false)
const isFrameBlocked = computed(() => location.protocol === 'https:' && currentApp.value?.url.startsWith('http:'))
const currentApp = computed(() => applications.find(app => app.id === page.value))
const { conversations, activeId, draft, identity, isSending, isLoginOpen, isLoggingIn, loginError, error, username, password, messageArea, messages, newConversation, selectConversation, send, signIn, signOut, closeLogin, stop } = useChat()
let frameTimer: ReturnType<typeof setTimeout> | undefined
function navigate(id: PageId) { page.value = id; location.hash = id === 'home' ? '/' : `/${id}` }
function syncHash() { page.value = pageFromHash(location.hash) }
window.addEventListener('hashchange', syncHash)
function resetFrame() {
  clearTimeout(frameTimer)
  isFrameLoading.value = !isFrameBlocked.value
  isFrameSlow.value = false
  if (!isFrameBlocked.value) frameTimer = setTimeout(() => { isFrameLoading.value = false; isFrameSlow.value = true }, 12000)
}
function loadedFrame() { clearTimeout(frameTimer); isFrameLoading.value = false; isFrameSlow.value = false }
function reloadFrame() { frameKey.value++; resetFrame() }
watch(page, () => { if (currentApp.value) resetFrame() }, { immediate: true })
onBeforeUnmount(() => { window.removeEventListener('hashchange', syncHash); clearTimeout(frameTimer) })
function startConversation() { newConversation(); navigate('home') }
function openConversation(id: string) { selectConversation(id); navigate('home') }
function usePrompt(value: string) { draft.value = value }
function handleKey(event: Event | KeyboardEvent) {
  if (!(event instanceof KeyboardEvent)) return
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); void send() }
}
function renderMarkdown(content: string): string { return DOMPurify.sanitize(marked.parse(content, { async: false }), { FORBID_TAGS: ['img'] }) }
const prompts = ['帮我梳理一份产品知识文档', '如何评估智能体的回答质量？', '帮我制定团队知识治理计划']
</script>

<template>
  <LoginView v-if="!loggedIn" />
  <div v-else class="portal" :class="{ collapsed: isCollapsed }">
    <aside class="sidebar" aria-label="门户导航">
      <a class="brand" href="#/" aria-label="杰克科技 AIGC 门户首页" @click="navigate('home')">
        <span class="brand-mark">J<span>·</span></span>
        <span class="brand-copy"><strong>杰克科技</strong><small>JACK TECHNOLOGY</small></span>
      </a>
      <div class="workspace-label">AIGC 工作空间</div>
      <button class="new-chat" aria-label="开启新对话" title="开启新对话" @click="startConversation"><el-icon><Plus /></el-icon><span>开启新对话</span><kbd>+</kbd></button>
      <nav>
        <button class="nav-item" :class="{ active: page === 'home' }" :aria-current="page === 'home' ? 'page' : undefined" title="与小杰对话" @click="navigate('home')"><el-icon><MessageCircle /></el-icon><span>与小杰对话</span><span class="nav-dot" /></button>
        <div class="nav-section">应用中心</div>
        <button v-for="app in applications" :key="app.id" class="nav-item" :class="{ active: page === app.id }" :aria-current="page === app.id ? 'page' : undefined" :title="app.name" @click="navigate(app.id)"><el-icon><component :is="lucideIconMap[app.icon]" /></el-icon><span>{{ app.name }}</span><el-icon class="nav-arrow"><ArrowUpRight /></el-icon></button>
      </nav>
      <section class="history" aria-label="本次访问的对话">
        <div class="nav-section">最近对话 <el-tooltip content="对话仅保留在当前页面，刷新后清空"><span class="history-note">本次访问</span></el-tooltip></div>
        <p v-if="!conversations.length" class="no-history">从一个好问题开始</p>
        <button v-for="item in conversations" :key="item.id" class="history-item" :class="{ selected: item.id === activeId && page === 'home' }" :title="item.title" @click="openConversation(item.id)"><el-icon><MessageCircle /></el-icon><span>{{ item.title }}</span></button>
      </section>
      <div class="sidebar-bottom">
        <div class="brand-note"><span class="note-line" /><p>连接知识 · 激发创造</p><small>让智能融入每一天的工作</small></div>
        <div class="account-row">
          <button class="account" aria-label="企业账号" title="企业账号" @click="isLoginOpen = true"><span class="avatar">{{ identity ? identity.user.username.slice(0, 1).toUpperCase() : '杰' }}</span><span class="account-copy"><strong>{{ identity?.user.username || '欢迎来到杰克科技' }}</strong><small>{{ identity ? '企业账号' : '登录后与小杰对话' }}</small></span><el-icon><LogIn /></el-icon></button>
          <el-tooltip content="退出门户登录" placement="top"><button class="logout-button" aria-label="退出门户登录" title="退出门户登录" @click="logoutPortal"><el-icon><LogOut /></el-icon></button></el-tooltip>
        </div>
      </div>
    </aside>

    <main class="main">
      <header class="topbar">
        <div class="topbar-left"><el-tooltip :content="isCollapsed ? '展开导航' : '收起导航'"><button class="icon-button" :aria-label="isCollapsed ? '展开导航' : '收起导航'" @click="isCollapsed = !isCollapsed"><el-icon><PanelLeftOpen v-if="isCollapsed" /><PanelLeftClose v-else /></el-icon></button></el-tooltip><span class="topbar-divider" /><span>{{ currentApp?.name || '小杰 · 智能助手' }}</span></div>
        <span class="topbar-brand">杰克科技 <span>/</span> AIGC 门户</span>
      </header>

      <section v-if="page === 'home'" class="chat-page" :class="{ 'has-messages': messages.length }">
        <div v-if="!messages.length" class="welcome">
          <div class="eyebrow"><span /> YOUR AI WORKSPACE</div>
          <div class="mascot" aria-hidden="true"><div class="mascot-face"><i /><i /><span /></div><el-icon class="mascot-spark"><Sparkles /></el-icon></div>
          <h1>你好，我是<span>小杰</span></h1>
          <p class="welcome-subtitle">你的工作好搭档，让每一个想法更进一步。</p>
        </div>
        <div v-else ref="messageArea" class="message-area" role="log" aria-label="与小杰的对话" aria-live="polite" :aria-busy="isSending">
          <article v-for="(message, index) in messages" :key="index" class="message" :class="message.role">
            <span v-if="message.role === 'assistant'" class="assistant-avatar"><el-icon><Sparkles /></el-icon></span>
            <div class="message-body"><strong v-if="message.role === 'assistant'" class="message-author">小杰</strong><div v-if="message.role === 'user'" class="user-text">{{ message.content }}</div><div v-else class="markdown" v-html="renderMarkdown(message.content)" />
              <details v-if="message.citations?.length" class="citations"><summary>参考资料 · {{ message.citations.length }}</summary><div v-for="(citation, i) in message.citations" :key="i"><strong>{{ citation.document_title || `资料 ${i + 1}` }}</strong><p>{{ citation.content }}</p></div></details>
            </div>
          </article>
          <div v-if="isSending" class="thinking"><el-icon><Sparkles /></el-icon><span>小杰正在查找资料、组织回答</span><span class="thinking-dots">···</span></div>
        </div>

        <div class="composer-area">
          <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="chat-error" />
          <form class="composer" @submit.prevent="send">
            <el-input v-model="draft" type="textarea" :rows="3" resize="none" maxlength="8000" placeholder="把问题交给小杰，让工作更简单…" aria-label="向小杰提问" @keydown="handleKey" />
            <div class="composer-toolbar"><el-tooltip content="通过知识治理专家的企业问答服务生成回答"><span class="model-label"><el-icon><Sparkles /></el-icon>小杰 · 企业智能助手</span></el-tooltip><div class="send-controls"><span class="keyboard-hint">Enter 发送 · Shift + Enter 换行</span><el-tooltip v-if="isSending" content="停止等待回答"><el-button class="send-button" type="primary" aria-label="停止等待回答" :icon="Square" @click="stop" /></el-tooltip><el-tooltip v-else content="发送消息"><el-button class="send-button" type="primary" native-type="submit" aria-label="发送消息" :disabled="!draft.trim()" :icon="ArrowUp" /></el-tooltip></div></div>
          </form>
          <div v-if="!messages.length" class="suggestions"><button v-for="prompt in prompts" :key="prompt" @click="usePrompt(prompt)">{{ prompt }}<el-icon><ArrowUpRight /></el-icon></button></div>
        </div>

        <section v-if="!messages.length" class="application-section" aria-label="快捷应用入口"><div class="section-heading"><span>从这里，开启更多可能</span><span>探索企业 AI 应用</span></div><div class="application-grid"><button v-for="(app, index) in applications" :key="app.id" class="application-card" @click="navigate(app.id)"><div class="card-top"><span class="application-icon"><el-icon><component :is="lucideIconMap[app.icon]" /></el-icon></span><span class="card-number">0{{ index + 1 }}</span></div><div class="card-title">{{ app.name }}<el-icon><ArrowUpRight /></el-icon></div><p>{{ app.description }}</p></button></div></section>
        <footer class="chat-footer">小杰与你一起，智造更多可能<span>AI 生成内容仅供参考，请核实重要信息</span></footer>
      </section>

      <section v-else-if="currentApp" class="app-page">
        <div class="app-toolbar"><div><strong>{{ currentApp.name }}</strong><span>{{ currentApp.caption }}</span></div><div class="app-actions"><el-tooltip content="若内嵌页面空白或登录受限，请在新窗口打开"><span class="embed-hint">独立应用</span></el-tooltip><el-button :icon="RotateCw" @click="reloadFrame">刷新页面</el-button><a :href="currentApp.url" target="_blank" rel="noopener noreferrer" class="external-link"><el-icon><ExternalLink /></el-icon>新窗口打开</a></div></div>
        <el-alert v-if="isFrameSlow" title="页面加载时间较长，请检查内网连接，或点击「新窗口打开」。" type="warning" show-icon :closable="false" />
        <div v-if="isFrameBlocked || (authConfig?.provider === 'keycloak' && currentApp.id === 'knowledge')" class="blocked-frame"><el-icon :size="40"><ExternalLink /></el-icon><h2>在新窗口继续使用{{ currentApp.name }}</h2><p>{{ isFrameBlocked ? '当前门户使用 HTTPS，浏览器不允许内嵌此 HTTP 应用。' : '知识治理专家将使用同一企业登录会话，在独立窗口打开。' }}</p><a :href="currentApp.url" target="_blank" rel="noopener noreferrer">打开{{ currentApp.name }}<el-icon><ArrowUpRight /></el-icon></a></div>
        <div v-else v-loading="isFrameLoading" element-loading-text="正在打开应用…" class="frame-wrap"><iframe :key="`${currentApp.id}-${frameKey}`" :src="currentApp.url" :title="currentApp.name" referrerpolicy="no-referrer" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals" @load="loadedFrame" /></div>
      </section>
    </main>

    <el-dialog v-model="isLoginOpen" :title="identity ? '企业账号' : '登录，与小杰开始对话'" width="min(440px, 92vw)" @closed="closeLogin">
      <template v-if="identity"><p>当前账号：{{ identity.user.username }}</p><p v-if="authConfig?.provider === 'keycloak'"><a :href="authConfig.account_url" target="_blank" rel="noopener noreferrer">管理我的账号</a><a v-if="['admin', 'super_admin'].includes(identity.user.role || '')" :href="authConfig.admin_url" target="_blank" rel="noopener noreferrer" style="margin-left: 24px">管理统一账号</a></p><el-button :icon="LogOut" @click="signOut(); isLoginOpen = false">退出登录</el-button></template>
      <template v-else-if="authConfig?.provider === 'local'"><p class="login-description">使用知识治理专家的企业账号登录。</p><el-alert v-if="loginError" :title="loginError" type="error" show-icon :closable="false" /><el-form label-position="top" @submit.prevent="signIn"><el-form-item label="账号"><el-input v-model="username" autocomplete="username" placeholder="请输入企业账号" @keyup.enter="signIn" /></el-form-item><el-form-item label="密码"><el-input v-model="password" type="password" show-password autocomplete="current-password" placeholder="请输入密码" @keyup.enter="signIn" /></el-form-item><el-button type="primary" class="login-submit" :loading="isLoggingIn" :disabled="!username.trim() || !password" @click="signIn">登录并继续</el-button></el-form><a class="login-help" :href="applications[0].url" target="_blank" rel="noopener noreferrer">前往知识治理专家管理账号 <el-icon><ArrowUpRight /></el-icon></a></template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.portal { display: flex; height: 100dvh; min-height: 560px; overflow: hidden; }
.sidebar { width: 248px; flex-shrink: 0; background: var(--app-white); border-right: 1px solid var(--el-border-color-lighter); display: flex; flex-direction: column; padding: 32px 20px 16px; transition: width .2s, padding .2s; }
.brand { display: flex; align-items: center; gap: 12px; color: var(--app-ink); margin: 0 8px 32px; }
.brand-mark { width: 40px; height: 44px; border-radius: 12px; background: var(--app-brand-blue); color: var(--app-white); font-size: 32px; font-weight: 800; font-style: italic; display: flex; align-items: center; justify-content: center; letter-spacing: -4px; padding-right: 4px; }
.brand-mark span { color: var(--app-blue-200); }
.brand-copy { display: grid; gap: 5px; white-space: nowrap; strong { font-size: 20px; letter-spacing: 3px; } small { font-size: 9px; letter-spacing: 1.2px; color: var(--el-text-color-secondary); } }
.workspace-label, .nav-section { color: var(--el-text-color-secondary); font-size: 11px; letter-spacing: 1px; }
.workspace-label { padding: 0 12px; margin-bottom: 16px; }
.new-chat { width: 100%; height: 44px; display: flex; align-items: center; gap: 12px; border: 1px solid var(--app-blue-100); background: var(--app-blue-50); color: var(--app-brand-blue); border-radius: 10px; padding: 0 14px; margin-bottom: 24px; font-size: 14px; font-weight: 500; }
.new-chat kbd { margin-left: auto; font-size: 16px; font-weight: 400; opacity: .5; }
.nav-item { display: flex; align-items: center; gap: 12px; padding: 0 14px; width: 100%; height: 46px; border: none; border-radius: 8px; background: transparent; color: var(--el-text-color-regular); text-align: left; font-size: 14px; margin-bottom: 4px; white-space: nowrap; .el-icon { font-size: 18px; } }
.nav-item:hover, .history-item:hover { background: var(--app-canvas); }
.nav-item.active { color: var(--app-brand-blue); background: var(--app-blue-50); font-weight: 600; }
.nav-dot { display: none; width: 6px; height: 6px; border-radius: 50%; background: var(--app-brand-blue); margin-left: auto; }
.active .nav-dot { display: block; }
.nav-arrow { margin-left: auto; opacity: 0; }.nav-item:hover .nav-arrow { opacity: .6; }
.nav-section { margin: 28px 14px 12px; display: flex; align-items: center; justify-content: space-between; white-space: nowrap; }
.history-note { font-size: 10px; letter-spacing: 0; opacity: .7; cursor: help; }.history { flex: 1; min-height: 60px; overflow: auto; }.no-history { margin: 20px 14px; font-size: 12px; color: var(--el-text-color-placeholder); }
.history-item { display: flex; align-items: center; gap: 8px; width: 100%; padding: 10px 14px; border: 0; border-radius: 6px; background: transparent; color: var(--el-text-color-regular); font-size: 12px; text-align: left; span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; } .el-icon { flex-shrink: 0; } &.selected { background: var(--app-blue-50); } }
.sidebar-bottom { margin-top: 16px; }.brand-note { padding: 20px 12px; margin-bottom: 12px; p { font-size: 12px; color: var(--el-text-color-regular); margin: 12px 0 6px; } small { color: var(--el-text-color-secondary); font-size: 10px; } }.note-line { display: block; height: 2px; width: 24px; background: var(--app-blue-200); }
.account { display: flex; align-items: center; gap: 10px; border: 0; background: transparent; width: 100%; min-width: 0; text-align: left; color: var(--el-text-color-secondary); }
.account-row { display: flex; align-items: center; gap: 12px; border-top: 1px solid var(--el-border-color-lighter); padding: 18px 4px 0; }
.logout-button { width: 34px; height: 34px; flex-shrink: 0; display: grid; place-items: center; border: 0; border-radius: 8px; background: var(--app-canvas); color: var(--el-text-color-secondary); font-size: 15px; }
.logout-button:hover { color: var(--app-brand-blue); background: var(--app-blue-50); }.avatar { width: 34px; height: 34px; flex-shrink: 0; background: var(--app-blue-50); color: var(--app-brand-blue); display: grid; place-items: center; border-radius: 50%; font-size: 13px; }.account-copy { display: grid; gap: 5px; flex: 1; min-width: 0; strong { color: var(--el-text-color-regular); font-size: 12px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; } small { font-size: 10px; } }
.main { flex: 1; min-width: 0; display: flex; flex-direction: column; background: var(--app-white); }.topbar { height: 72px; flex-shrink: 0; padding: 0 32px; display: flex; justify-content: space-between; align-items: center; font-size: 13px; }.topbar-left { display: flex; align-items: center; gap: 16px; }.icon-button { display: grid; place-items: center; border: 0; background: transparent; color: var(--el-text-color-secondary); width: 28px; height: 28px; font-size: 18px; }.topbar-divider { width: 1px; height: 16px; background: var(--el-border-color-lighter); }.topbar-brand { font-size: 11px; color: var(--el-text-color-secondary); letter-spacing: 1px; span { margin: 0 10px; color: var(--el-border-color); } }
.chat-page { flex: 1; min-height: 0; display: flex; flex-direction: column; align-items: center; overflow-y: auto; padding: 24px 40px 16px; background: radial-gradient(ellipse at 50% 25%, var(--app-blue-50) 0, transparent 65%); }.welcome { text-align: center; margin: auto 0 28px; padding-top: 12px; }.eyebrow { display: flex; align-items: center; justify-content: center; gap: 8px; color: var(--el-text-color-secondary); font-size: 10px; letter-spacing: 3px; margin-bottom: 24px; span { width: 5px; height: 5px; border-radius: 50%; background: var(--app-brand-blue); } }
.mascot { position: relative; width: 72px; height: 72px; border-radius: 24px; background: linear-gradient(140deg, var(--app-blue-100), var(--app-brand-blue)); margin: 0 auto 20px; display: grid; place-items: center; box-shadow: 0 8px 24px var(--app-blue-100), inset 0 2px 2px var(--app-white); transform: rotate(-8deg); }.mascot-face { width: 48px; height: 36px; background: var(--app-white); border-radius: 14px; display: flex; justify-content: center; gap: 12px; padding-top: 10px; position: relative; i { height: 9px; width: 5px; background: var(--app-brand-blue); border-radius: 4px; } > span { position: absolute; width: 10px; height: 5px; border-bottom: 2px solid var(--app-brand-blue); border-radius: 0 0 8px 8px; bottom: 8px; } }.mascot-spark { position: absolute; color: var(--app-brand-blue); right: -14px; top: -10px; font-size: 24px; }
h1 { margin: 0; font-size: 36px; font-weight: 600; letter-spacing: 1px; span { color: var(--app-brand-blue); margin-left: 8px; } }.welcome-subtitle { margin: 14px 0 0; font-size: 14px; color: var(--el-text-color-secondary); letter-spacing: .5px; }.composer-area, .application-section { width: min(100%, 840px); flex-shrink: 0; }.composer { border: 1px solid var(--app-blue-200); border-radius: 16px; background: var(--app-white); padding: 18px 20px 14px; box-shadow: var(--app-shadow); transition: border-color .2s; &:focus-within { border-color: var(--app-brand-blue); } :deep(.el-textarea__inner) { background: transparent; box-shadow: none; padding: 0; line-height: 1.7; font-size: 14px; color: var(--el-text-color-primary); } }.composer-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 12px; }.model-label { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--el-text-color-regular); .el-icon { color: var(--app-brand-blue); font-size: 16px; } }.send-controls { display: flex; align-items: center; gap: 16px; }.keyboard-hint { color: var(--el-text-color-placeholder); font-size: 10px; }.send-button { width: 36px; height: 36px; padding: 0; border-radius: 10px; font-size: 20px; }.suggestions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; button { display: flex; align-items: center; gap: 8px; background: var(--app-white); border: 1px solid var(--el-border-color-lighter); border-radius: 20px; padding: 8px 12px; font-size: 11px; color: var(--el-text-color-regular); &:hover { color: var(--app-brand-blue); border-color: var(--app-blue-200); } .el-icon { color: var(--el-text-color-secondary); } } }
.application-section { margin-top: 40px; }.section-heading { display: flex; justify-content: space-between; color: var(--el-text-color-regular); font-size: 12px; margin-bottom: 16px; > span:last-child { font-size: 10px; color: var(--el-text-color-secondary); } }.application-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }.application-card { background: var(--app-white); border: 1px solid var(--el-border-color-lighter); border-radius: 12px; padding: 20px; text-align: left; transition: border-color .2s, transform .2s; &:hover { border-color: var(--app-blue-200); transform: translateY(-2px); } p { color: var(--el-text-color-secondary); font-size: 11px; margin: 10px 0 0; line-height: 1.6; } }.card-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }.application-icon { display: grid; place-items: center; height: 36px; width: 36px; border-radius: 10px; color: var(--app-brand-blue); background: var(--app-blue-50); font-size: 19px; }.card-number { font-size: 11px; letter-spacing: 1px; color: var(--el-text-color-placeholder); }.card-title { display: flex; justify-content: space-between; align-items: center; font-size: 14px; font-weight: 600; color: var(--el-text-color-primary); .el-icon { font-size: 16px; color: var(--el-text-color-secondary); } }.chat-footer { padding-top: 32px; margin-top: auto; color: var(--el-text-color-secondary); font-size: 10px; text-align: center; letter-spacing: 1px; > span { display: block; margin-top: 8px; font-size: 9px; color: var(--el-text-color-placeholder); letter-spacing: 0; } }
.has-messages { background: var(--app-white); padding-top: 0; .chat-footer { margin-top: 0; padding-top: 16px; } }.message-area { flex: 1; min-height: 0; overflow-y: auto; width: min(100%, 840px); padding: 24px 8px; margin-bottom: 16px; }.message { display: flex; gap: 12px; margin-bottom: 28px; }.message.user { justify-content: flex-end; }.user .message-body { max-width: 85%; }.user-text { white-space: pre-wrap; overflow-wrap: anywhere; background: var(--app-blue-50); border-radius: 16px 16px 4px 16px; padding: 12px 18px; font-size: 14px; line-height: 1.8; }.assistant-avatar { background: var(--app-blue-50); color: var(--app-brand-blue); width: 32px; height: 32px; border-radius: 10px; flex-shrink: 0; display: grid; place-items: center; }.message-body { min-width: 0; }.message-author { display: block; font-size: 13px; padding-top: 6px; margin-bottom: 12px; }.markdown { font-size: 14px; line-height: 1.8; overflow-wrap: anywhere; :deep(p) { margin-top: 0; } :deep(pre) { overflow: auto; padding: 16px; background: var(--app-canvas); border-radius: 8px; } :deep(table) { display: block; overflow: auto; border-collapse: collapse; } :deep(td), :deep(th) { padding: 8px; border: 1px solid var(--el-border-color); } }.citations { font-size: 12px; color: var(--el-text-color-secondary); summary { cursor: pointer; } > div { margin-top: 12px; padding: 12px; background: var(--app-canvas); border-radius: 8px; } p { max-height: 160px; overflow: auto; white-space: pre-wrap; } }.thinking { display: flex; align-items: center; gap: 10px; color: var(--el-text-color-secondary); font-size: 12px; .el-icon { color: var(--app-brand-blue); } }.thinking-dots { font-size: 24px; animation: pulse 1.2s infinite; } @keyframes pulse { 50% { opacity: .3; } }.chat-error { margin-bottom: 12px; }
.app-page { flex: 1; min-height: 0; display: flex; flex-direction: column; border-top: 1px solid var(--el-border-color-lighter); }.app-toolbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 16px 24px; font-size: 13px; > div:first-child { display: flex; align-items: center; gap: 16px; > span { color: var(--el-text-color-secondary); font-size: 12px; } } }.app-actions { display: flex; align-items: center; gap: 16px; flex-shrink: 0; }.external-link { display: flex; align-items: center; gap: 6px; }.embed-hint { color: var(--el-text-color-secondary); font-size: 11px; cursor: help; }.frame-wrap { flex: 1; min-height: 0; }iframe { width: 100%; height: 100%; border: 0; display: block; background: var(--app-canvas); }.blocked-frame { margin: auto; text-align: center; padding: 32px; color: var(--el-text-color-secondary); h2 { color: var(--el-text-color-primary); font-size: 20px; } p { font-size: 14px; } a { display: inline-flex; align-items: center; gap: 8px; margin-top: 16px; } }.login-description { color: var(--el-text-color-secondary); margin: 0 0 24px; font-size: 13px; }.login-submit { width: 100%; }.login-help { margin-top: 20px; display: flex; align-items: center; justify-content: center; gap: 4px; font-size: 12px; }:deep(.el-form) { margin-top: 20px; }
.collapsed { .sidebar { width: 80px; padding-left: 12px; padding-right: 12px; }.brand { margin: 0 auto 32px; }.brand-copy, .workspace-label, .new-chat span, .new-chat kbd, .nav-item > span, .nav-item .nav-dot, .nav-arrow, .nav-section, .history, .brand-note, .account-copy, .account > .el-icon { display: none; }.new-chat { justify-content: center; padding: 0; }.nav-item { justify-content: center; padding: 0; }.sidebar-bottom { margin-top: auto; }.account-row { justify-content: center; }.account { justify-content: center; } }
@media (max-height: 850px) and (min-width: 761px) { .chat-page { padding-top: 12px; }.welcome { margin-bottom: 24px; }.eyebrow { margin-bottom: 16px; }.mascot { width: 60px; height: 60px; margin-bottom: 16px; }.application-section { margin-top: 28px; }.application-card { padding: 16px; }.card-top { margin-bottom: 12px; }.chat-footer { padding-top: 24px; } }
@media (max-height: 760px) and (min-width: 761px) { .topbar { height: 64px; }.welcome { margin-bottom: 20px; padding-top: 0; }.eyebrow { margin-bottom: 12px; }.mascot { height: 52px; width: 52px; border-radius: 18px; margin-bottom: 12px; }h1 { font-size: 32px; }.welcome-subtitle { margin-top: 10px; }.application-section { margin-top: 24px; }.application-card { padding: 12px 16px; }.chat-footer { padding-top: 16px; }.brand-note { display: none; } }
@media (max-width: 1100px) { .sidebar { width: 224px; padding-left: 16px; padding-right: 16px; }.chat-page { padding-left: 28px; padding-right: 28px; }.keyboard-hint { display: none; }.application-grid { gap: 12px; }.application-card { padding: 16px; }.app-toolbar > div:first-child > span, .embed-hint { display: none; } }
@media (max-width: 760px) { .portal { min-height: 480px; }.sidebar, .collapsed .sidebar { width: 64px; padding: 20px 8px 12px; }.brand, .collapsed .brand { margin: 0 auto 24px; }.brand-mark { width: 36px; height: 40px; }.brand-copy, .workspace-label, .new-chat span, .new-chat kbd, .nav-item > span, .nav-item .nav-dot, .nav-arrow, .nav-section, .history, .brand-note, .account-copy, .account > .el-icon { display: none; }.new-chat { justify-content: center; padding: 0; }.nav-item { justify-content: center; padding: 0; margin-bottom: 12px; }.sidebar-bottom { margin-top: auto; }.account { justify-content: center; }.topbar { height: 56px; padding: 0 16px; }.topbar-brand, .topbar-left .icon-button, .topbar-divider { display: none; }.chat-page { padding: 24px 16px 16px; }.welcome { padding-top: 12px; margin-bottom: 24px; }h1 { font-size: 28px; }.welcome-subtitle { font-size: 12px; line-height: 1.7; max-width: 250px; }.eyebrow { font-size: 8px; letter-spacing: 2px; }.composer { padding: 14px; }.model-label { font-size: 10px; gap: 4px; }.suggestions { gap: 6px; button { font-size: 10px; padding: 7px 9px; } }.application-section { margin-top: 28px; }.section-heading > span:last-child { display: none; }.application-grid { grid-template-columns: 1fr; gap: 8px; }.application-card { padding: 12px; display: grid; grid-template-columns: 36px 1fr; gap: 4px 12px; align-items: center; p { grid-column: 2; margin: 0; font-size: 10px; } }.card-top { grid-row: span 2; margin: 0; }.card-number { display: none; }.card-title { font-size: 12px; }.chat-footer { line-height: 1.6; }.app-toolbar { align-items: flex-start; flex-direction: column; padding: 12px; gap: 10px; }.app-actions { gap: 12px; font-size: 12px; }.blocked-frame { padding: 16px; }.has-messages { padding-top: 0; } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; } }
</style>
