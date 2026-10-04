<script setup lang="ts">
import { ref } from 'vue'
import { ElAlert, ElButton, ElForm, ElFormItem, ElIcon, ElInput } from 'element-plus'
import { ArrowRight, KeyRound, Sparkles, UserRound } from '@lucide/vue'
import heroUrl from '../assets/login-hero.jpg'
import { authConfig, authError, isAuthReady, initializeAuth, signIn } from '../auth/session'

const username = ref<string>('')
const password = ref<string>('')
const error = ref<string>('')
const isSubmitting = ref<boolean>(false)

async function submit() {
  if (isSubmitting.value) return
  isSubmitting.value = true
  error.value = ''
  try { await signIn(username.value.trim(), password.value); password.value = '' }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '登录失败，请重试。' }
  finally { isSubmitting.value = false }
}
</script>

<template>
  <main class="login-view">
    <section class="login-panel" aria-label="门户登录">
      <div class="login-brand">
        <span class="brand-mark">J<span>·</span></span>
        <span class="brand-copy"><strong>杰克科技</strong><small>JACK TECHNOLOGY</small></span>
      </div>
      <div class="login-form-wrap">
        <p class="login-eyebrow"><span /> AIGC 智能门户</p>
        <h1>你好，欢迎回来</h1>
        <p class="login-subtitle">登录杰克科技 AIGC 门户，与小杰一起开启智能工作。</p>
        <el-alert v-if="error || authError" :title="error || authError" type="error" show-icon :closable="false" class="login-error" />
        <el-button v-if="authError" :loading="!isAuthReady" @click="initializeAuth">重试连接</el-button>
        <el-form v-if="authConfig" class="login-form" label-position="top" @submit.prevent="submit">
          <el-form-item v-if="authConfig.provider === 'local'" label="账号">
            <el-input v-model="username" size="large" autocomplete="username" placeholder="请输入账号" :prefix-icon="UserRound" @keyup.enter="submit" />
          </el-form-item>
          <el-form-item v-if="authConfig.provider === 'local'" label="密码">
            <el-input v-model="password" size="large" type="password" show-password autocomplete="current-password" placeholder="请输入密码" :prefix-icon="KeyRound" @keyup.enter="submit" />
          </el-form-item>
          <el-button class="login-submit" type="primary" size="large" native-type="submit" :loading="isSubmitting" :disabled="authConfig.provider === 'local' && (!username.trim() || !password)">
            {{ authConfig.provider === 'keycloak' ? '使用企业统一账号登录' : '登录' }}<el-icon class="submit-arrow"><ArrowRight /></el-icon>
          </el-button>
        </el-form>
        <p class="login-note">内部系统，请勿泄露账号信息 · 如遇问题请联系门户管理员</p>
        <p class="login-hint"><el-icon><Sparkles /></el-icon>门户与知识治理专家共用企业账号</p>
      </div>
    </section>
    <section class="hero-panel" aria-label="门户介绍">
      <img :src="heroUrl" alt="AIGC 门户概念插画" />
      <div class="hero-overlay">
        <p class="hero-eyebrow"><span /> 杰克科技 · AIGC 门户</p>
        <h2>连接知识 · 激发创造</h2>
        <p>小杰智能助手、知识治理专家、智能体测评与 SkillHub，一站式企业 AI 工作空间。</p>
      </div>
    </section>
  </main>
</template>

<style scoped lang="scss">
.login-view { display: grid; grid-template-columns: minmax(420px, 44%) 1fr; height: 100dvh; min-height: 600px; background: var(--app-white); }
.login-panel { display: flex; flex-direction: column; padding: 40px 48px 32px; overflow-y: auto; }
.login-brand { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
.brand-mark { width: 40px; height: 44px; border-radius: 12px; background: var(--app-brand-blue); color: var(--app-white); font-size: 32px; font-weight: 800; font-style: italic; display: flex; align-items: center; justify-content: center; letter-spacing: -4px; padding-right: 4px; }
.brand-mark span { color: var(--app-blue-200); }
.brand-copy { display: grid; gap: 5px; white-space: nowrap; strong { font-size: 20px; letter-spacing: 3px; color: var(--app-ink); } small { font-size: 9px; letter-spacing: 1.2px; color: var(--el-text-color-secondary); } }
.login-form-wrap { width: min(100%, 380px); margin: auto; padding: 40px 0 28px; }
.login-eyebrow { display: flex; align-items: center; gap: 10px; color: var(--el-text-color-secondary); font-size: 12px; letter-spacing: 2px; margin: 0 0 20px; span { width: 20px; height: 2px; background: var(--app-brand-blue); } }
h1 { margin: 0 0 10px; font-size: 30px; font-weight: 700; letter-spacing: 1px; color: var(--app-ink); }
.login-subtitle { margin: 0 0 28px; font-size: 14px; line-height: 1.7; color: var(--el-text-color-secondary); }
.login-error { margin-bottom: 18px; }
.login-form :deep(.el-form-item) { margin-bottom: 22px; }
.login-form :deep(.el-form-item__label) { font-size: 13px; color: var(--el-text-color-regular); }
.login-submit { width: 100%; font-weight: 600; letter-spacing: 2px; .submit-arrow { margin-left: 6px; font-size: 16px; } }
.login-note { margin: 28px 0 0; font-size: 11px; line-height: 1.8; color: var(--el-text-color-placeholder); }
.login-hint { display: flex; align-items: center; gap: 6px; margin: 6px 0 0; font-size: 11px; color: var(--el-text-color-secondary); .el-icon { font-size: 13px; color: var(--app-brand-blue); } }
.hero-panel { position: relative; overflow: hidden; background: var(--app-blue-900); }
.hero-panel img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.hero-panel::after { content: ''; position: absolute; inset: 0; background: linear-gradient(180deg, rgb(26 64 153 / 12%) 0%, rgb(26 64 153 / 58%) 100%); }
.hero-overlay { position: absolute; left: 52px; right: 52px; bottom: 46px; color: var(--app-white); z-index: 1; max-width: 520px; }
.hero-eyebrow { display: flex; align-items: center; gap: 10px; margin: 0 0 18px; font-size: 12px; letter-spacing: 2px; opacity: .9; span { width: 22px; height: 2px; background: var(--app-white); } }
.hero-overlay h2 { margin: 0 0 12px; font-size: 34px; font-weight: 700; letter-spacing: 2px; }
.hero-overlay p:last-child { margin: 0; font-size: 14px; line-height: 1.8; opacity: .92; }
@media (max-width: 900px) {
  .login-view { grid-template-columns: 1fr; grid-template-rows: 210px 1fr; min-height: 560px; }
  .hero-panel { order: -1; }
  .hero-overlay { display: none; }
  .login-panel { padding: 28px 24px 24px; }
  .login-form-wrap { padding: 28px 0 20px; }
  h1 { font-size: 26px; }
}
@media (prefers-reduced-motion: reduce) { .login-view, .login-form-wrap { transition: none; } }
</style>
