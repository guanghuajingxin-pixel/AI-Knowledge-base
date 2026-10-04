<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElAlert, ElButton, ElCard, ElDescriptions, ElDescriptionsItem, ElIcon, vLoading } from 'element-plus'
import { ExternalLink, Users } from '@lucide/vue'
import { useUserStore } from '@/stores/user'
import request from '@/api/request'

const props = defineProps<{ administration?: boolean }>()
const userStore = useUserStore()
const isLoading = ref<boolean>(true)
const error = ref<string>('')
const targetUrl = ref<string>('')
onMounted(async () => {
  try {
    const links = await request.get<unknown, { account_url: string; admin_url: string | null }>('/auth/account-management')
    targetUrl.value = (props.administration ? links.admin_url : links.account_url) || ''
    if (!targetUrl.value) error.value = '当前账号没有统一账号管理权限，请联系管理员。'
  } catch { error.value = '账号中心暂不可用，请刷新页面重试。' }
  finally { isLoading.value = false }
})
</script>

<template>
  <section v-loading="isLoading" class="unified-account">
    <el-card shadow="never">
      <template #header><span class="panel-title"><el-icon><Users /></el-icon>{{ administration ? '统一账号管理' : '我的企业账号' }}</span></template>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <el-descriptions v-else :column="1" border>
        <el-descriptions-item label="当前账号">{{ userStore.userInfo?.username }}</el-descriptions-item>
        <el-descriptions-item label="身份中心">杰克科技 · Keycloak</el-descriptions-item>
        <el-descriptions-item label="适用系统">AIGC 门户、知识治理专家</el-descriptions-item>
      </el-descriptions>
      <p>{{ administration ? '在统一账号中心新增、停用用户，分配角色或重置密码。' : '在账号中心维护个人资料、修改密码和管理登录会话。' }}</p>
      <a v-if="targetUrl" :href="targetUrl" target="_blank" rel="noopener noreferrer"><el-button type="primary" :icon="ExternalLink">{{ administration ? '打开账号管理' : '打开账号中心' }}</el-button></a>
    </el-card>
  </section>
</template>

<style scoped lang="scss">
.unified-account { min-height: 320px; padding: 24px; }
.panel-title { display: inline-flex; align-items: center; gap: 8px; font-weight: 600; }
p { color: var(--el-text-color-secondary); font-size: 14px; margin: 24px 0 16px; }
</style>
