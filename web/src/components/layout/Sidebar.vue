<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import { ChevronDown, Stamp } from '@lucide/vue'
import { resolveLucideIcon } from '@/utils/lucide-icons'
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { getMenuVisibility, getSiteBranding } from '@/api/settings'
import { openParserApiGuide } from '@/utils/api-docs'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const userStore = useUserStore()

const userRole = computed(() => userStore.userInfo?.role)

// 菜单显示配置：后端配置的隐藏菜单路径列表（默认空 = 全部显示）
const hiddenMenus = ref<string[]>([])
// 站点外观：名称与 Logo（系统配置可改；空 = 默认「知识治理专家」+ 默认图标）
const siteName = ref('')
const siteLogo = ref('')
async function loadSiteBranding() {
  try {
    const site = await getSiteBranding()
    siteName.value = site.site_name || ''
    siteLogo.value = site.site_logo || ''
  } catch {
    // 站点外观读取失败时保持默认
  }
}

function onBrandingChanged() {
  loadSiteBranding()
}

onMounted(async () => {
  // 两个请求互不依赖，并发拉取：侧边栏是首屏必现内容，串行会白等一个来回
  await Promise.all([
    getMenuVisibility()
      .then((res) => { hiddenMenus.value = res.hidden || [] })
      .catch(() => { hiddenMenus.value = [] }),  // 读取失败时回退为全部显示，不阻塞侧边栏
    loadSiteBranding(),
  ])
  // 系统配置页保存站点名称/图标后即时刷新（免刷新页面）
  window.addEventListener('site-branding-changed', onBrandingChanged)
})

onUnmounted(() => {
  window.removeEventListener('site-branding-changed', onBrandingChanged)
})

interface MenuItem {
  path: string
  title: string
  icon: string
  group: string
  parent?: string
  roles?: string[]
  menuOrder?: number
}

interface MenuGroup {
  key: string
  parentTitle: string
  parentIcon: string
  children: MenuItem[]
}

const allMenuItems = computed<MenuItem[]>(() =>
  router
    .getRoutes()
    .filter((r) => r.meta?.title && !r.meta?.hidden && r.name)
    .map((r) => ({
      path: r.path,
      title: r.meta!.title as string,
      icon: r.meta!.icon as string,
      group: (r.meta!.group as string) || 'feature',
      parent: r.meta!.parent as string | undefined,
      roles: r.meta!.roles as string[] | undefined,
      menuOrder: r.meta!.menuOrder as number | undefined,
    })),
)

// 过滤角色可见的菜单项
function filterByRole(items: MenuItem[]): MenuItem[] {
  return items.filter((item) => {
    const r = router.getRoutes().find((rr) => rr.path === item.path)
    const roles = r?.meta?.roles as string[] | undefined
    if (roles) return !!userRole.value && roles.includes(userRole.value)
    return true
  })
}

// 功能区：feature 组（含二级分组，受菜单显示配置控制）
const featureMenuGroups = computed<MenuGroup[]>(() => {
  const items = filterByRole(allMenuItems.value.filter(
    (i) => i.group === 'feature' && !hiddenMenus.value.includes(i.path),
  ))
  const groupMap = new Map<string, MenuGroup>()
  const parents: MenuItem[] = []
  const children: MenuItem[] = []

  for (const item of items) {
    if (item.parent) {
      children.push(item)
    } else {
      parents.push(item)
    }
  }

  // 父项先创建分组
  for (const p of parents) {
    groupMap.set(p.path, {
      key: p.path,
      parentTitle: p.title,
      parentIcon: p.icon,
      children: [],
    })
  }

  // 子项归入对应父分组（menuOrder 显式排序，未设置时保持路由顺序）
  for (const child of children) {
    if (child.parent && groupMap.has(child.parent)) {
      groupMap.get(child.parent)!.children.push(child)
    }
  }
  for (const group of groupMap.values()) {
    group.children.sort((a, b) => (a.menuOrder ?? 999) - (b.menuOrder ?? 999))
  }

  // 没有子项的分组降级为单独项（children 为空时仍以分组形式展示但不展开）
  return Array.from(groupMap.values())
})

// 所有独立功能菜单项（没有 parent 的，受菜单显示配置控制）
const standaloneFeatureItems = computed(() => {
  const items = filterByRole(allMenuItems.value.filter(
    (i) => i.group === 'feature' && !i.parent && !hiddenMenus.value.includes(i.path),
  ))
  return items.filter((item) => !featureMenuGroups.value.some((g) => g.key === item.path))
})

// 平台配置：config + admin 组
const configMenuItems = computed(() =>
  allMenuItems.value.filter((item) => {
    if (item.group !== 'config' && item.group !== 'admin') return false
    if (item.parent) return false // 配置区暂不支持二级分组
    const r = router.getRoutes().find((rr) => rr.path === item.path)
    const roles = r?.meta?.roles as string[] | undefined
    if (roles) return !!userRole.value && roles.includes(userRole.value)
    return true
  }),
)

// 父分组展开状态（未设置时由 isGroupExpanded 兜底默认展开）
const expandedGroups = ref<Record<string, boolean>>({})
function toggleGroup(key: string) {
  // 基于当前有效显示状态取反：首次点击未写入过的分组也要正确收起，
  // 不能用 !undefined（= true），否则首次收起需要点两下
  expandedGroups.value[key] = !isGroupExpanded(key)
}
function isGroupExpanded(key: string): boolean {
  if (expandedGroups.value[key] !== undefined) return expandedGroups.value[key]
  // 默认展开有子项的分组
  const group = featureMenuGroups.value.find((g) => g.key === key)
  return group ? group.children.length > 0 : false
}

function isActive(path: string): boolean {
  return route.path === path || route.path.startsWith(path + '/')
}

function isGroupActive(groupKey: string): boolean {
  // 父分组自身路径活跃 或 任一子项活跃
  if (isActive(groupKey)) return true
  const group = featureMenuGroups.value.find((g) => g.key === groupKey)
  if (!group) return false
  return group.children.some((c) => isActive(c.path))
}

function navigate(path: string) {
  if (route.path !== path) {
    router.push(path)
  }
}

// 「关于我」产品介绍页
const aboutUrl = `${import.meta.env.BASE_URL}about.html`

// 左下角用户区菜单：关于我（新窗口）/ API 调用说明（新页签）/ 退出登录
function handleUserCommand(command: string | number | object) {
  if (command === 'about') {
    window.open(aboutUrl, '_blank', 'noopener')
  } else if (command === 'api-docs') {
    // 解析引擎 API 调用说明文档页（自编静态页，含端到端示例），与处理引擎页入口同源
    openParserApiGuide()
  } else if (command === 'logout') {
    userStore.logout()
    router.push('/login')
  }
}
</script>

<template>
  <aside class="sidebar" :class="{ collapsed: appStore.sidebarCollapsed }">
    <div class="logo" @click="router.push('/chat')">
      <div class="logo-icon">
        <img v-if="siteLogo" :src="siteLogo" alt="站点图标" />
        <el-icon v-else :size="22" color="#fff"><Stamp /></el-icon>
      </div>
      <span v-show="!appStore.sidebarCollapsed" class="logo-text">{{ siteName || '知识治理专家' }}</span>
    </div>

    <nav class="menu-container">
      <!-- 功能区 -->
      <div class="menu-group-label" v-show="!appStore.sidebarCollapsed">功能区</div>
      <div class="menu-group">
        <!-- 二级分组项 -->
        <template v-for="group in featureMenuGroups" :key="group.key">
          <!-- 有子项的分组 -->
          <div
            v-if="group.children.length > 0"
            class="menu-group-wrapper"
          >
            <!-- 父级行：点击整行展开/收起，右侧内嵌箭头指示状态 -->
            <div
              class="menu-item menu-item--parent"
              :class="{ active: isGroupActive(group.key) }"
              @click="toggleGroup(group.key)"
              @contextmenu.prevent
            >
              <span class="menu-icon">
                <el-icon :size="16">
                  <component :is="resolveLucideIcon(group.parentIcon)" />
                </el-icon>
              </span>
              <span v-show="!appStore.sidebarCollapsed" class="menu-title">{{ group.parentTitle }}</span>
              <el-icon
                v-show="!appStore.sidebarCollapsed"
                class="menu-expand"
                :class="{ expanded: isGroupExpanded(group.key) }"
              >
                <ChevronDown />
              </el-icon>
            </div>
            <!-- 子项列表 -->
            <transition name="submenu">
              <div v-show="isGroupExpanded(group.key) && !appStore.sidebarCollapsed" class="submenu">
                <div
                  v-for="child in group.children"
                  :key="child.path"
                  class="menu-item menu-item--child"
                  :class="{ active: isActive(child.path) }"
                  :title="child.title"
                  @click="navigate(child.path)"
                >
                  <span class="menu-icon menu-icon--child">
                    <el-icon :size="14">
                      <component :is="resolveLucideIcon(child.icon)" />
                    </el-icon>
                  </span>
                  <span class="menu-title">{{ child.title }}</span>
                </div>
              </div>
            </transition>
          </div>

          <!-- 无子项的分组（降级为普通项） -->
          <div
            v-else
            class="menu-item"
            :class="{ active: isActive(group.key) }"
            :title="group.parentTitle"
            @click="navigate(group.key)"
          >
            <span class="menu-icon">
              <el-icon :size="16">
                <component :is="resolveLucideIcon(group.parentIcon)" />
              </el-icon>
            </span>
            <span v-show="!appStore.sidebarCollapsed" class="menu-title">{{ group.parentTitle }}</span>
          </div>
        </template>

        <!-- 独立功能菜单项 -->
        <div
          v-for="item in standaloneFeatureItems"
          :key="item.path"
          class="menu-item"
          :class="{ active: isActive(item.path) }"
          :title="item.title"
          @click="navigate(item.path)"
        >
          <span class="menu-icon">
            <el-icon :size="16">
              <component :is="resolveLucideIcon(item.icon)" />
            </el-icon>
          </span>
          <span v-show="!appStore.sidebarCollapsed" class="menu-title">{{ item.title }}</span>
        </div>
      </div>

      <!-- 平台配置 -->
      <template v-if="configMenuItems.length > 0">
        <div class="menu-divider" />
        <div class="menu-group-label" v-show="!appStore.sidebarCollapsed">平台配置</div>
        <div class="menu-group">
          <div
            v-for="item in configMenuItems"
            :key="item.path"
            class="menu-item"
            :class="{ active: isActive(item.path) }"
            :title="item.title"
            @click="navigate(item.path)"
          >
            <span class="menu-icon">
              <el-icon :size="16">
                <component :is="resolveLucideIcon(item.icon)" />
              </el-icon>
            </span>
            <span v-show="!appStore.sidebarCollapsed" class="menu-title">{{ item.title }}</span>
          </div>
        </div>
      </template>
    </nav>

    <!-- 左下角：用户区（关于我 / API 调用说明 / 退出登录） -->
    <div class="sidebar-footer">
      <el-dropdown class="user-dropdown" trigger="click" @command="handleUserCommand">
        <span
          class="menu-item user-item"
          :title="userStore.userInfo?.username || '用户'"
          role="button"
          :aria-label="`用户菜单：${userStore.userInfo?.username || '用户'}`"
        >
          <el-avatar :size="28" icon="UserFilled" class="user-avatar" />
          <span v-show="!appStore.sidebarCollapsed" class="menu-title username">
            {{ userStore.userInfo?.username || '用户' }}
          </span>
          <el-icon v-show="!appStore.sidebarCollapsed" class="menu-expand"><ChevronDown /></el-icon>
        </span>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="about">关于我</el-dropdown-item>
            <el-dropdown-item command="api-docs">API 调用说明</el-dropdown-item>
            <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 220px;
  background: #fff;
  border-right: 1px solid #e8e8e8;
  display: flex;
  flex-direction: column;
  transition: width 0.3s ease;
  overflow: hidden;
  flex-shrink: 0;
}

.sidebar.collapsed {
  width: 64px;
}

.logo {
  height: 60px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 16px;
  cursor: pointer;
  flex-shrink: 0;
}

.logo-icon {
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--app-brand-blue), #6d28d9);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  overflow: hidden;
}
.logo-icon img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.logo-text {
  font-size: 15px;
  font-weight: 700;
  color: #1a1a2e;
  white-space: nowrap;
}

.menu-container {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.menu-group {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.menu-group-wrapper {
  position: relative;
}

.menu-divider {
  height: 1px;
  background: #f0f0f0;
  margin: 12px 8px;
}

.menu-group-label {
  font-size: 11px;
  color: #999;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding: 0 12px 6px;
  white-space: nowrap;
}

.menu-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s ease;
  white-space: nowrap;
}

.menu-item:hover {
  background: #f5f7fa;
}

.menu-item.active {
  background: var(--app-blue-50);
}

.menu-item.active .menu-title {
  color: var(--app-brand-blue);
  font-weight: 600;
}

.menu-item--parent {
  padding-right: 12px;
}

.menu-item--child {
  padding-left: 36px;
  padding-top: 8px;
  padding-bottom: 8px;
}

.menu-icon {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  background: #f0f0f0;
  color: #666;
  transition: background 0.15s ease, color 0.15s ease, transform 0.15s ease;
}

.menu-icon--child {
  width: 24px;
  height: 24px;
  border-radius: 6px;
}

.menu-item:hover .menu-icon {
  transform: scale(1.08);
}

.menu-item.active .menu-icon {
  background: var(--app-brand-blue);
  color: #fff;
}

.menu-expand {
  margin-left: auto;
  font-size: 12px;
  color: #999;
  padding: 4px;
  transition: transform 0.2s ease;
}

.menu-expand.expanded {
  transform: rotate(180deg);
}

.menu-item--parent:hover .menu-expand {
  color: var(--app-brand-blue);
}

.submenu {
  display: flex;
  flex-direction: column;
  gap: 1px;
  margin-top: 2px;
}

/* 展开动画 */
.submenu-enter-active,
.submenu-leave-active {
  transition: all 0.2s ease;
  overflow: hidden;
}
.submenu-enter-from,
.submenu-leave-to {
  opacity: 0;
  max-height: 0;
}
.submenu-enter-to,
.submenu-leave-from {
  opacity: 1;
  max-height: 300px;
}

.menu-title {
  font-size: 14px;
  color: #303133;
  transition: color 0.15s ease;
}

.menu-container::-webkit-scrollbar {
  width: 4px;
}

.menu-container::-webkit-scrollbar-thumb {
  background: #d9d9d9;
  border-radius: 2px;
}

.menu-container::-webkit-scrollbar-track {
  background: transparent;
}

/* 左下角用户区：关于我 / 退出登录 */
.sidebar-footer {
  flex-shrink: 0;
  padding: 8px;
  border-top: 1px solid #f0f0f0;
}

.user-dropdown {
  display: block;
  width: 100%;
}

.user-item {
  width: 100%;
  outline: none;
}

.user-avatar {
  flex-shrink: 0;
}

.username {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
