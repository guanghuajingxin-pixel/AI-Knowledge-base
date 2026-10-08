<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Copy, Eye, EyeOff } from '@lucide/vue'
import { copyToClipboard, revealProfileSecret } from '@/api/settings'

/**
 * 表格密钥单元格：默认展示后端脱敏值，眼睛图标切换显示完整密钥，拷贝图标复制完整密钥。
 * 完整密钥按需拉取（仅管理员接口），拉取结果缓存在本实例内。
 */
const props = defineProps<{
  /** 配置类型：dify / llm / ragflow / embedding / rerank */
  kind: 'dify' | 'llm' | 'ragflow' | 'embedding' | 'rerank'
  rowId: string
  /** 列表接口返回的脱敏回显值（如 sk-****3456） */
  masked: string
  hasKey: boolean
}>()

const revealed = ref(false)
const full = ref('')
const busy = ref(false)

async function ensureFull() {
  if (full.value) return full.value
  busy.value = true
  try {
    full.value = (await revealProfileSecret(props.kind, props.rowId)).api_key
    return full.value
  } finally {
    busy.value = false
  }
}

async function toggleReveal() {
  if (revealed.value) {
    revealed.value = false
    return
  }
  try {
    await ensureFull()
    revealed.value = true
  } catch {
    ElMessage.error('密钥读取失败，请稍后重试')
  }
}

async function copyKey() {
  try {
    const v = await ensureFull()
    if (!v) {
      ElMessage.warning('该配置未设置 API Key')
      return
    }
    await copyToClipboard(v)
    ElMessage.success('API Key 已复制')
  } catch {
    ElMessage.error('复制失败，请手动选择复制')
  }
}

const display = computed(() => (revealed.value ? full.value : props.masked) || '未设置')
</script>

<template>
  <div class="secret-key-cell">
    <span class="key-text" :title="display">{{ display }}</span>
    <template v-if="hasKey">
      <el-button
        link
        size="small"
        :loading="busy && !revealed"
        :aria-label="revealed ? '隐藏 API Key' : '显示 API Key'"
        @click="toggleReveal"
      >
        <el-icon><EyeOff v-if="revealed" /><Eye v-else /></el-icon>
      </el-button>
      <el-button link size="small" aria-label="复制 API Key" @click="copyKey">
        <el-icon><Copy /></el-icon>
      </el-button>
    </template>
  </div>
</template>

<style scoped>
.secret-key-cell {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  max-width: 100%;
}
.key-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}
</style>
