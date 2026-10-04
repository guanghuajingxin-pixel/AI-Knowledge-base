<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock, Reading } from '@element-plus/icons-vue'
import { getDingtalkConfig, type DingtalkConfig } from '@/api/auth'
import { gotoDingtalkAuth, loginDestination, safeRedirect } from '@/utils/dingtalk-auth'
import type { LoginResponse } from '@/types/user'
import { authConfig, authError, sso } from '@/auth/session'
import { ElAlert, ElButton } from 'element-plus'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()

const activeTab = ref('password')
const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({
  username: '',
  password: '',
})

const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

// 钉钉扫码配置：qr_login_enabled=true 时才展示「钉钉扫码」页签
const dtConfig = ref<DingtalkConfig | null>(null)
onMounted(async () => {
  if (authConfig.value?.provider !== 'local') return
  try {
    dtConfig.value = await getDingtalkConfig()
  } catch {
    // 配置读取失败（后端未配钉钉）：仅保留密码登录，不打扰用户
    dtConfig.value = null
  }
})

/** 登录成功统一落地：token/userInfo 入 store，需设密先去设密页，否则回原目标 */
function postLogin(res: LoginResponse) {
  userStore.applyLogin(res)
  const redirect = safeRedirect(route.query.redirect, '/chat')
  router.push(loginDestination(res, redirect))
}

async function handleLogin() {
  if (!formRef.value) return
  await formRef.value.validate(async (valid) => {
    if (!valid) return
    loading.value = true
    try {
      const res = await userStore.login(form)
      ElMessage.success('登录成功')
      postLogin(res)
    } catch (e) {
      // 登录接口 401 由 request.ts 放行至此；展示后端 detail 或默认提示
      const err = e as { response?: { data?: { detail?: string } } }
      ElMessage.error(err?.response?.data?.detail || '用户名或密码错误')
    } finally {
      loading.value = false
    }
  })
}

/** 钉钉扫码登录：整页跳转钉钉统一授权页，回调 /login/callback 换 token（不引钉钉 JS SDK） */
const dtLoading = ref(false)
function handleDingtalkLogin() {
  if (!dtConfig.value?.app_key) {
    ElMessage.error('钉钉应用未配置，请联系管理员')
    return
  }
  dtLoading.value = true
  try {
    gotoDingtalkAuth(dtConfig.value.app_key)
  } catch {
    dtLoading.value = false
    ElMessage.error('跳转钉钉授权页失败，请重试')
  }
}
function reloadPage() { window.location.reload() }
async function handleSsoLogin() {
  try { await sso.login(safeRedirect(route.query.redirect, '/chat')) }
  catch (error) { authError.value = error instanceof Error ? error.message : '统一登录失败，请重试。' }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <el-icon size="40" color="var(--el-color-primary)"><Reading /></el-icon>
        <h2>知识治理专家</h2>
      </div>
      <el-alert v-if="authError" :title="authError" type="error" :closable="false" show-icon />
      <div v-if="authConfig?.provider === 'keycloak'" class="dt-pane">
        <p class="dt-desc">使用杰克科技统一账号，与 AIGC 门户共享登录身份和权限。</p>
        <el-button type="primary" style="width: 100%" @click="handleSsoLogin">使用企业统一账号登录</el-button>
      </div>
      <el-button v-if="!authConfig" style="width: 100%; margin-top: 16px" @click="reloadPage">重试连接</el-button>
      <el-tabs v-if="authConfig?.provider === 'local'" v-model="activeTab" stretch>
        <el-tab-pane label="密码登录" name="password">
          <el-form
            ref="formRef"
            :model="form"
            :rules="rules"
            size="large"
            @keyup.enter="handleLogin"
          >
            <el-form-item prop="username">
              <el-input v-model="form.username" placeholder="用户名" :prefix-icon="User" />
            </el-form-item>
            <el-form-item prop="password">
              <el-input v-model="form.password" type="password" show-password placeholder="密码" :prefix-icon="Lock" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="loading" style="width: 100%" @click="handleLogin">
                登录
              </el-button>
            </el-form-item>
          </el-form>
          <div class="login-tip">默认管理员：admin / admin123</div>
        </el-tab-pane>
        <el-tab-pane v-if="dtConfig?.qr_login_enabled" label="钉钉扫码" name="dingtalk">
          <div class="dt-pane">
            <p class="dt-desc">点击下方按钮跳转钉钉统一授权页，使用钉钉 App 扫码确认后自动登录。</p>
            <el-button type="primary" size="large" :loading="dtLoading" style="width: 100%" @click="handleDingtalkLogin">
              使用钉钉扫码登录
            </el-button>
            <div class="login-tip">首次扫码将自动创建账号，并引导设置本地密码</div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}
.login-card {
  width: 400px;
  padding: 40px;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.15);
}
.login-header {
  text-align: center;
  margin-bottom: 16px;
}
.login-header h2 {
  margin: 12px 0 0;
  color: #303133;
}
.login-tip {
  text-align: center;
  color: #909399;
  font-size: 12px;
  margin-top: 8px;
}
.dt-pane {
  padding: 8px 0 4px;
}
.dt-desc {
  margin: 0 0 20px;
  color: #606266;
  font-size: 13px;
  line-height: 1.7;
  text-align: center;
}
</style>
