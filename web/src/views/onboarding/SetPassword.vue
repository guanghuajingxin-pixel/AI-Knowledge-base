<script setup lang="ts">
// 首次登录 / 管理员重置密码后：强制设置本地密码（独立居中布局，仿登录页）
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { Lock, Reading } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import { setPassword } from '@/api/auth'
import { checkPasswordStrength, safeRedirect } from '@/utils/dingtalk-auth'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({
  new_password: '',
  new_password_confirm: '',
})

const rules: FormRules = {
  new_password: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) => {
        const err = checkPasswordStrength(value)
        callback(err ? new Error(err) : undefined)
      },
      trigger: 'blur',
    },
  ],
  new_password_confirm: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) => {
        callback(value === form.new_password ? undefined : new Error('两次输入的密码不一致'))
      },
      trigger: 'blur',
    },
  ],
}

async function handleSubmit() {
  if (!formRef.value) return
  await formRef.value.validate(async (valid) => {
    if (!valid) return
    loading.value = true
    try {
      const res = await setPassword({
        new_password: form.new_password,
        new_password_confirm: form.new_password_confirm,
      })
      // 响应带新 token（旧 token 可能随设密失效），userInfo 以响应为准
      if (res.access_token) userStore.setToken(res.access_token)
      if (res.user) userStore.setUserInfo({ ...res.user, must_change_password: false })
      else userStore.patchUserInfo({ must_change_password: false })
      ElMessage.success('密码设置成功')
      router.replace(safeRedirect(route.query.redirect, '/chat'))
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } } }
      ElMessage.error(err?.response?.data?.detail || '密码设置失败，请重试')
    } finally {
      loading.value = false
    }
  })
}
</script>

<template>
  <div class="setpwd-page">
    <div class="setpwd-card">
      <div class="setpwd-header">
        <el-icon size="40" color="var(--el-color-primary)"><Reading /></el-icon>
        <h2>设置本地密码</h2>
      </div>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="首次登录请设置本地密码，用于钉钉不可用时登录"
        style="margin-bottom: 20px"
      />
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        size="large"
        label-position="top"
        @keyup.enter="handleSubmit"
      >
        <el-form-item prop="new_password" label="新密码">
          <el-input
            v-model="form.new_password"
            type="password"
            show-password
            placeholder="至少 8 位，须含字母和数字"
            :prefix-icon="Lock"
          />
        </el-form-item>
        <el-form-item prop="new_password_confirm" label="确认新密码">
          <el-input
            v-model="form.new_password_confirm"
            type="password"
            show-password
            placeholder="再次输入新密码"
            :prefix-icon="Lock"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" style="width: 100%" @click="handleSubmit">
            保存并继续
          </el-button>
        </el-form-item>
      </el-form>
      <div class="setpwd-tip">密码至少 8 位，须同时包含字母和数字</div>
    </div>
  </div>
</template>

<style scoped>
.setpwd-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}
.setpwd-card {
  width: 420px;
  padding: 40px;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.15);
}
.setpwd-header {
  text-align: center;
  margin-bottom: 20px;
}
.setpwd-header h2 {
  margin: 12px 0 0;
  color: #303133;
}
.setpwd-tip {
  text-align: center;
  color: #909399;
  font-size: 12px;
  margin-top: 4px;
}
</style>
