<script setup lang="ts">
/**
 * 知识库选库卡片（公共组件）——「添加知识库」弹窗卡片视图的三段式选卡：
 * 头部（类型图标+名称）/ 描述 / 页脚（类型标签+停用标记+分隔线+更新时间）。
 * 整卡可点选（原生 button + aria-pressed），停用库由 disabled 接管交互。
 */
import { Check } from '@element-plus/icons-vue'
import type { KnowledgeLibrary } from '@/api/knowledge-library'
import { formatDate } from '@/utils/format'
import { kbTypeLabel, kbTypeMeta } from './type-meta'

const props = defineProps<{ lib: KnowledgeLibrary; selected?: boolean }>()
const emit = defineEmits<{ (e: 'toggle'): void }>()

function onClick() {
  if (props.lib.enabled) emit('toggle')
}
</script>

<template>
  <button
    type="button"
    class="kb-pick-card"
    :class="{ 'is-selected': selected, 'is-disabled': !lib.enabled }"
    :aria-pressed="selected"
    :disabled="!lib.enabled"
    @click="onClick"
  >
    <span class="kb-pick-check" aria-hidden="true"><el-icon :size="11"><Check /></el-icon></span>
    <span class="pick-header">
      <span class="pick-icon" :style="{ color: kbTypeMeta(lib).color, background: kbTypeMeta(lib).bg }">
        <el-icon :size="18"><component :is="kbTypeMeta(lib).icon" /></el-icon>
      </span>
      <span class="pick-name" :title="lib.name">{{ lib.name }}</span>
    </span>
    <span class="pick-desc">{{ lib.description || '暂无描述' }}</span>
    <span class="pick-footer">
      <span class="pick-tag" :style="{ color: kbTypeMeta(lib).color, background: kbTypeMeta(lib).bg }">{{ kbTypeLabel(lib) }}</span>
      <span v-if="!lib.enabled" class="pick-tag is-off">已停用</span>
      <span class="pick-divider" aria-hidden="true"></span>
      <span class="pick-time">{{ formatDate(lib.updated_at) }}</span>
    </span>
  </button>
</template>

<style scoped>
.kb-pick-card {
  position: relative;
  min-width: 0;
  width: 100%;
  height: 148px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px;
  border: 1px solid #eef0f4;
  border-radius: 12px;
  background: #fff;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s, background-color 0.15s, box-shadow 0.2s, transform 0.2s;
  animation: kb-card-in 0.28s ease backwards;
}
.kb-pick-card:not(.is-disabled):hover {
  border-color: #d6ddf5;
  background: #fff;
  box-shadow: 0 6px 16px rgba(31, 56, 88, 0.1);
  transform: translateY(-2px);
}
.kb-pick-card.is-selected {
  border-color: var(--el-color-primary);
  background: #fff;
  box-shadow: 0 0 0 3px rgba(64, 158, 255, 0.12);
}
.kb-pick-card.is-disabled {
  cursor: not-allowed;
  opacity: 0.62;
}
.kb-pick-card:focus-visible {
  outline: none;
  border-color: var(--el-color-primary);
  box-shadow: 0 0 0 3px rgba(64, 158, 255, 0.22);
}
.kb-pick-check {
  position: absolute;
  top: 12px;
  right: 12px;
  display: none;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--el-color-primary);
  color: #fff;
  font-size: 11px;
  line-height: 1;
}
.kb-pick-card.is-selected .kb-pick-check {
  display: inline-flex;
  animation: kb-check-in 0.18s ease;
}
.pick-header {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.pick-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 9px;
}
.pick-name {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  line-height: 1.4;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kb-pick-card.is-disabled .pick-name {
  color: #909399;
}
.pick-desc {
  font-size: 12.5px;
  color: #909399;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  /* 固定两行高度，空描述时不塌陷、布局稳定 */
  min-height: 38px;
}
.pick-footer {
  margin-top: auto;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.pick-tag {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 48px;
  height: 20px;
  padding: 0 8px;
  border-radius: 4px;
  font-size: 11px;
  line-height: 1;
}
.pick-tag.is-off {
  color: #909399;
  background: #f4f4f5;
}
.pick-divider {
  flex-shrink: 0;
  width: 1px;
  height: 12px;
  background: #e4e7ed;
}
.pick-time {
  font-size: 11.5px;
  color: #c0c4cc;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
@keyframes kb-card-in {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
@keyframes kb-check-in {
  from {
    transform: scale(0.4);
    opacity: 0;
  }
  to {
    transform: scale(1);
    opacity: 1;
  }
}
@media (prefers-reduced-motion: reduce) {
  .kb-pick-card {
    animation: none;
  }
  .kb-pick-card:not(.is-disabled):hover {
    transform: none;
  }
  .kb-pick-card.is-selected .kb-pick-check {
    animation: none;
  }
}
</style>
