<script setup lang="ts">
import { useTabsStore } from '@/stores/tabs'
import { useAppStore } from '@/stores/app'
import { useRoute, useRouter } from 'vue-router'
import { Close, Expand, Fold } from '@element-plus/icons-vue'
import { ref, onMounted, onUnmounted } from 'vue'

const tabsStore = useTabsStore()
const appStore = useAppStore()
const route = useRoute()
const router = useRouter()

const contextMenu = ref({
  visible: false,
  x: 0,
  y: 0,
  targetPath: '',
})

function isActive(path: string): boolean {
  return route.path === path
}

function handleClick(path: string) {
  router.push(path)
}

function handleClose(e: MouseEvent, path: string) {
  e.stopPropagation()
  tabsStore.removeTab(path)
}

function handleContextMenu(e: MouseEvent, path: string) {
  e.preventDefault()
  contextMenu.value = {
    visible: true,
    x: e.clientX,
    y: e.clientY,
    targetPath: path,
  }
}

function closeCurrent() {
  tabsStore.removeTab(contextMenu.value.targetPath)
  hideContextMenu()
}

function closeOthers() {
  tabsStore.closeOthers(contextMenu.value.targetPath)
  hideContextMenu()
}

function closeAll() {
  tabsStore.closeAll()
  hideContextMenu()
}

function hideContextMenu() {
  contextMenu.value.visible = false
}

function onDocClick() {
  hideContextMenu()
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
})

onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
})
</script>

<template>
  <div class="tab-bar">
    <!-- 侧边栏折叠按钮（原顶栏功能，顶栏移除后迁至此处） -->
    <el-icon class="collapse-btn" size="18" role="button" aria-label="收起或展开侧边栏" @click="appStore.toggleSidebar()">
      <Fold v-if="!appStore.sidebarCollapsed" />
      <Expand v-else />
    </el-icon>
    <div class="tab-list">
      <div
        v-for="tab in tabsStore.tabs"
        :key="tab.path"
        class="tab-item"
        :class="{ active: isActive(tab.path) }"
        @click="handleClick(tab.path)"
        @contextmenu="handleContextMenu($event, tab.path)"
      >
        <span class="tab-title">{{ tab.title }}</span>
        <span class="tab-close" @click="handleClose($event, tab.path)">
          <el-icon :size="12"><Close /></el-icon>
        </span>
      </div>
    </div>

    <!-- Context Menu -->
    <Teleport to="body">
      <div
        v-show="contextMenu.visible"
        class="tab-context-menu"
        :style="{ left: contextMenu.x + 'px', top: contextMenu.y + 'px' }"
      >
        <div class="context-item" @click="closeCurrent">关闭当前</div>
        <div class="context-item" @click="closeOthers">关闭其它</div>
        <div class="context-item" @click="closeAll">关闭所有</div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.tab-bar {
  height: 40px;
  background: #fff;
  border-bottom: 1px solid #e8e8e8;
  display: flex;
  align-items: stretch;
  padding: 0 8px;
  flex-shrink: 0;
  user-select: none;
  gap: 10px;
}

.collapse-btn {
  align-self: center;
  cursor: pointer;
  flex-shrink: 0;
  color: #606266;
}

.tab-list {
  display: flex;
  align-items: flex-end;
  gap: 0;
  overflow-x: auto;
  flex: 1;
}

.tab-list::-webkit-scrollbar {
  height: 2px;
}

.tab-list::-webkit-scrollbar-thumb {
  background: #d9d9d9;
  border-radius: 1px;
}

.tab-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  font-size: 13px;
  color: #666;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  white-space: nowrap;
  transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
  border-radius: 4px 4px 0 0;
  position: relative;
}

.tab-item:hover {
  color: #303133;
  background: #f5f7fa;
}

.tab-item.active {
  color: var(--el-color-primary);
  border-bottom-color: var(--el-color-primary);
  font-weight: 500;
}

.tab-title {
  line-height: 1;
}

.tab-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  color: #999;
  transition: background 0.15s ease, color 0.15s ease;
}

.tab-close:hover {
  background: #d9d9d9;
  color: #333;
}
</style>

<style>
.tab-context-menu {
  position: fixed;
  z-index: 9999;
  background: #fff;
  border-radius: 6px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.12);
  padding: 4px 0;
  min-width: 120px;
}

.context-item {
  padding: 8px 16px;
  font-size: 13px;
  color: #303133;
  cursor: pointer;
  transition: background 0.15s ease;
}

.context-item:hover {
  background: #f5f7fa;
}
</style>
