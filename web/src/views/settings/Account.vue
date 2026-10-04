<script setup lang="ts">
// 个人账户：基本信息 / 钉钉绑定（扫码授权后由 /login/callback 绑定分支回跳） / 修改密码
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import PageContainer from '@/components/common/PageContainer.vue'
import { useUserStore } from '@/stores/user'
import { changePassword, getDingtalkConfig } from '@/api/auth'
import { gotoDingtalkAuth, checkPasswordStrength } from '@/utils/dingtalk-auth'
import { formatDate } from '@/utils/format'
import type { UserRole } from '@/types/user'
import { authConfig } from '@/auth/session'
import UnifiedAccountPanel from '@/components/common/unified-account-panel.vue'

const router = useRouter()
const userStore = useUserStore()

const roleMap: Record<UserRole, { label: string; type: 'danger' | 'warning' | 'success' | 'info' }> = {
  super_admin: { label: '超级管理员', type: 'danger' },
  admin: { label: '管理员', type: 'warning' },
  editor: { label: '编辑者', type: 'success' },
  viewer: { label: '查看者', type: 'info' },
}

const info = computed(() => userStore.userInfo)
const binding = computed(() => info.value?.dingtalk_binding || null)
const roleTag = computed(() => (info.value ? roleMap[info.value.role] : null))

onMounted(async () => {
  try {
    // 拉最新用户信息（绑定状态 / 角色可能已在别处变更）
    await userStore.fetchUserInfo()
  } catch {
    // 拦截器已提示；保留本地缓存展示
  }
})

// ===== 钉钉绑定 =====
const bindLoading = ref(false)
async function handleBindDingtalk() {
  bindLoading.value = true
  try {
    const cfg = await getDingtalkConfig()
    if (!cfg.app_key) {
      ElMessage.error('钉钉应用未配置，请联系管理员')
      return
    }
    // 整页跳转钉钉授权页；回调时 store 已有 token → 走绑定分支，成功后回本页
    gotoDingtalkAuth(cfg.app_key)
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err?.response?.data?.detail || '获取钉钉配置失败，请稍后重试')
    bindLoading.value = false
  }
}

// ===== 修改密码 =====
const pwdFormRef = ref<FormInstance>()
const pwdLoading = ref(false)
const pwdForm = reactive({
  old_password: '',
  new_password: '',
  new_password_confirm: '',
})

const pwdRules: FormRules = {
  old_password: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
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
        callback(value === pwdForm.new_password ? undefined : new Error('两次输入的密码不一致'))
      },
      trigger: 'blur',
    },
  ],
}

async function handleChangePassword() {
  if (!pwdFormRef.value) return
  await pwdFormRef.value.validate(async (valid) => {
    if (!valid) return
    pwdLoading.value = true
    try {
      await changePassword({
        old_password: pwdForm.old_password,
        new_password: pwdForm.new_password,
        new_password_confirm: pwdForm.new_password_confirm,
      })
      ElMessage.success('密码已修改，请使用新密码重新登录')
      // 旧 token 可能已失效：强制登出回登录页
      userStore.logout()
      router.push('/login')
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } } }
      ElMessage.error(err?.response?.data?.detail || '密码修改失败，请检查当前密码')
    } finally {
      pwdLoading.value = false
    }
  })
}
</script>

<template>
  <UnifiedAccountPanel v-if="authConfig?.provider === 'keycloak'" />
  <PageContainer v-else title="个人账户">
    <div class="account-wrap">
      <!-- 基本信息 -->
      <el-card shadow="never">
        <template #header><span class="card-title">基本信息</span></template>
        <el-descriptions :column="2" border>
          <el-descriptions-item label="用户名">{{ info?.username || '—' }}</el-descriptions-item>
          <el-descriptions-item label="角色">
            <el-tag v-if="roleTag" :type="roleTag.type" size="small">{{ roleTag.label }}</el-tag>
            <span v-else>—</span>
          </el-descriptions-item>
          <el-descriptions-item label="邮箱">{{ info?.email || '—' }}</el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ formatDate(info?.created_at) }}</el-descriptions-item>
        </el-descriptions>
      </el-card>

      <!-- 钉钉绑定 -->
      <el-card shadow="never" style="margin-top: 16px">
        <template #header><span class="card-title">钉钉绑定</span></template>
        <template v-if="binding">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="钉钉姓名">{{ binding.dt_name || '—' }}</el-descriptions-item>
            <el-descriptions-item label="dt_userid">
              <span class="mono">{{ binding.dt_userid || '—' }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="绑定时间">{{ formatDate(binding.bound_at) }}</el-descriptions-item>
          </el-descriptions>
          <div class="field-hint" style="margin-top: 8px">
            已绑定钉钉身份：知识库同步将以你本人的钉钉权限执行。如需解绑请联系管理员。
          </div>
        </template>
        <template v-else>
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="尚未绑定钉钉"
            description="绑定后，你创建的知识库同步将使用你本人的钉钉身份与权限；未绑定时同步将回退到全局服务账号。"
            style="margin-bottom: 16px"
          />
          <el-button type="primary" :loading="bindLoading" @click="handleBindDingtalk">绑定钉钉</el-button>
        </template>
      </el-card>

      <!-- 修改密码 -->
      <el-card shadow="never" style="margin-top: 16px">
        <template #header><span class="card-title">修改密码</span></template>
        <el-form
          ref="pwdFormRef"
          :model="pwdForm"
          :rules="pwdRules"
          label-width="100px"
          style="max-width: 460px"
        >
          <el-form-item label="当前密码" prop="old_password">
            <el-input v-model="pwdForm.old_password" type="password" show-password placeholder="请输入当前密码" />
          </el-form-item>
          <el-form-item label="新密码" prop="new_password">
            <el-input v-model="pwdForm.new_password" type="password" show-password placeholder="至少 8 位，须含字母和数字" />
          </el-form-item>
          <el-form-item label="确认新密码" prop="new_password_confirm">
            <el-input v-model="pwdForm.new_password_confirm" type="password" show-password placeholder="再次输入新密码" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="pwdLoading" @click="handleChangePassword">保存新密码</el-button>
            <span class="field-hint" style="margin-left: 12px">修改成功后需使用新密码重新登录</span>
          </el-form-item>
        </el-form>
      </el-card>
    </div>
  </PageContainer>
</template>

<style scoped>
.account-wrap {
  max-width: 860px;
}
.card-title {
  font-weight: 600;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
.field-hint {
  font-size: 12px;
  color: #909399;
}
</style>
