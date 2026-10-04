import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

export interface Tab {
  path: string
  title: string
}

// 关闭页签后无页签可停留时的回落页
const DEFAULT_PATH = '/chat'

// 页签持久化：刷新浏览器保留已打开的页签（sessionStorage 在浏览器标签关闭后自动清理）
const STORAGE_KEY = 'kge.tabs'

function loadTabs(): Tab[] {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((t) => t && typeof t.path === 'string' && t.path.length > 0)
      .map((t) => ({ path: t.path, title: typeof t.title === 'string' && t.title ? t.title : t.path }))
  } catch {
    return []
  }
}

function saveTabs(list: Tab[]) {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(list))
  } catch {
    // 隐私模式或存储被禁用：静默降级为仅内存态
  }
}

export const useTabsStore = defineStore('tabs', () => {
  const route = useRoute()
  const router = useRouter()

  const tabs = ref<Tab[]>(loadTabs())

  function addTab(path: string, title: string) {
    const existing = tabs.value.find((t) => t.path === path)
    if (!existing) {
      tabs.value.push({ path, title })
    }
  }

  // 动态页签标题：详情页加载到实体名后更新（如文档分段页显示文件名）
  function updateTabTitle(path: string, title: string) {
    if (!title) return
    const tab = tabs.value.find((t) => t.path === path)
    if (tab) tab.title = title
  }

  // 序号页签标题：固定 base 文案，同类页签多开时自动编号 -1/-2…（取最小可用编号；
  // 刷新恢复会话时已分配且不冲突的编号保持不变）。base 须为不含正则特殊字符的字面量。
  function assignSequentialTitle(path: string, base: string) {
    const slotOf = (t: string): number | null => {
      const m = new RegExp(`^${base}(-\\d+)?$`).exec(t)
      return m ? (m[1] ? Number(m[1]) : 0) : null
    }
    const cur = tabs.value.find((t) => t.path === path)
    const curSlot = cur ? slotOf(cur.title) : null
    const usedSlots = tabs.value
      .filter((t) => t.path !== path)
      .map((t) => slotOf(t.title))
      .filter((n): n is number => n !== null)
    if (curSlot !== null && !usedSlots.includes(curSlot)) return
    const used = new Set(usedSlots)
    let n = 0
    while (used.has(n)) n++
    updateTabTitle(path, n === 0 ? base : `${base}-${n}`)
  }

  function removeTab(path: string) {
    const tab = tabs.value.find((t) => t.path === path)
    if (!tab) return

    const idx = tabs.value.indexOf(tab)
    tabs.value.splice(idx, 1)

    // If closed tab was active, navigate to nearest sibling
    if (route.path === path || route.path.startsWith(path + '/') || route.path.startsWith(path + '?')) {
      const next = tabs.value[idx] || tabs.value[idx - 1]
      router.push(next ? next.path : DEFAULT_PATH)
    }
  }

  function closeOthers(path: string) {
    tabs.value = tabs.value.filter((t) => t.path === path)
  }

  function closeAll() {
    tabs.value = []
    router.push(DEFAULT_PATH)
  }

  // 持久化：tabs 任何变化都同步写回 sessionStorage
  watch(
    tabs,
    (list) => {
      saveTabs(list)
    },
    { deep: true },
  )

  // 页签级组件缓存（AppLayout 的 KeepAlive include 数据源）：
  // 记录每个页签路径对应的页面组件名（懒加载组件首次渲染后由 vue-router 回写到 route 记录上）。
  // 页签开着 → 组件名在 include 内 → 切换页签组件不销毁，进行中的检索/流式等事务不中断；
  // 页签关闭 → 组件名移出 include → 缓存随之释放，重新打开恢复全新挂载。
  const componentNames = ref<Record<string, string>>({})
  const cacheNames = computed(() => {
    const names = new Set<string>()
    for (const t of tabs.value) {
      const name = componentNames.value[t.path]
      if (name) names.add(name)
    }
    return [...names]
  })

  // Auto-add tabs on route change
  watch(
    () => route.path,
    (path) => {
      if (path === '/' || path === '/login') return
      // 记录该路径的页面组件名（KeepAlive 按组件名匹配缓存）
      const leaf = route.matched[route.matched.length - 1]
      const comp = leaf?.components?.default as { name?: string; __name?: string } | undefined
      const name = comp?.name || comp?.__name
      if (name) componentNames.value[path] = name
      // Find matched route to get title
      const matched = route.matched.filter((r) => r.meta?.title)
      const title = matched.length > 0 ? (matched[matched.length - 1].meta!.title as string) : path
      addTab(path, title)
    },
    { immediate: true },
  )

  return { tabs, cacheNames, addTab, updateTabTitle, assignSequentialTitle, removeTab, closeOthers, closeAll }
})
