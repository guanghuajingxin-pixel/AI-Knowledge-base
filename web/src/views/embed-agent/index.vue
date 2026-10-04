<script setup lang="ts">
// 通用外部平台智能体嵌入页（HiAgent / Dify 共用）
// 嵌入地址来自「智能体配置」页 external_agents[platform]，切换智能体仅需在配置页替换嵌入代码
import { ref, computed, onMounted, watch } from 'vue'
import { Loading } from '@element-plus/icons-vue'
import { getAgentConfig } from '@/api/agent'
import { useUserStore } from '@/stores/user'

const props = defineProps<{ platform: 'hiagent' | 'dify' }>()

const PLATFORM_TITLE: Record<'hiagent' | 'dify', string> = {
  hiagent: 'HiAgent',
  dify: 'Dify',
}
const title = PLATFORM_TITLE[props.platform]

const userStore = useUserStore()
const loading = ref(true) // 智能体配置加载中
const frameLoading = ref(true) // 嵌入页 iframe 加载中
const cfg = ref<{ enabled: boolean; url: string }>({ enabled: false, url: '' })
const loadFailed = ref(false)

const isAdmin = computed(() =>
  ['super_admin', 'admin'].includes(userStore.userInfo?.role || ''),
)

async function loadConfig() {
  loading.value = true
  frameLoading.value = true
  loadFailed.value = false
  try {
    const res = await getAgentConfig()
    const ext = res.external_agents?.[props.platform]
    cfg.value = {
      enabled: !!ext?.enabled && !!ext?.url,
      url: ext?.url || '',
    }
  } catch {
    loadFailed.value = true
  } finally {
    loading.value = false
  }
}

onMounted(loadConfig)

// /hiagent 与 /dify 复用同一组件实例，切换平台时必须按新 platform 重新读取对应配置
watch(() => props.platform, loadConfig)
</script>

<template>
  <div class="embed-agent-page">
    <!-- 配置加载中 -->
    <div v-if="loading" class="embed-mask">
      <el-icon class="is-loading" :size="28" color="var(--app-brand-blue)"><Loading /></el-icon>
      <span class="mask-text">正在加载 {{ title }} 智能问答…</span>
    </div>

    <!-- 配置加载失败 -->
    <div v-else-if="loadFailed" class="embed-mask">
      <el-result icon="warning" :title="`${title} 智能问答不可用`" sub-title="智能体配置加载失败，请稍后重试">
        <template #extra>
          <el-button type="primary" @click="() => $router.go(0)">刷新重试</el-button>
        </template>
      </el-result>
    </div>

    <!-- 未启用 / 未配置 -->
    <div v-else-if="!cfg.enabled" class="embed-mask">
      <el-result
        icon="info"
        :title="`${title} 智能体未启用`"
        sub-title="请联系管理员在「智能体配置」页选择该平台页签，粘贴平台嵌入代码并启用。"
      >
        <template #extra>
          <el-button v-if="isAdmin" type="primary" @click="$router.push('/agent-config')">前往配置</el-button>
        </template>
      </el-result>
    </div>

    <!-- 嵌入页面 -->
    <template v-else>
      <div v-if="frameLoading" class="embed-mask">
        <el-icon class="is-loading" :size="28" color="var(--app-brand-blue)"><Loading /></el-icon>
        <span class="mask-text">正在加载 {{ title }} 智能问答…</span>
      </div>
      <iframe
        :src="cfg.url"
        class="embed-frame"
        frameborder="0"
        allow="microphone;clipboard-write"
        @load="frameLoading = false"
      />
    </template>
  </div>
</template>

<style scoped>
.embed-agent-page {
  position: relative;
  height: 100%;
  min-height: 0;
  background: #fff;
}
.embed-frame {
  display: block;
  width: 100%;
  height: 100%;
  border: none;
}
.embed-mask {
  position: absolute;
  inset: 0;
  z-index: 2;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  background: #fff;
}
.mask-text {
  font-size: 14px;
  color: #6b7280;
}
</style>
