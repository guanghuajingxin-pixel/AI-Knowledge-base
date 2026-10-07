/**
 * 菜单图标注册表(统一 @lucide/vue)
 *
 * 事实源:route meta.icon 只允许出现这里的 key(key 与 lucide 导出名一致,PascalCase)。
 * 新增菜单图标必须同步登记;动态图标统一走 resolveLucideIcon,禁止在模板里 namespace import 现查。
 * 约定见 docs/frontend-dev-spec.md §2.4。
 */
import {
  BadgeCheck, ChartLine, ChevronDown, CircleHelp, Clock, Cpu, Download, EyeOff,
  FolderOpen, Gauge, KeyRound, Library, ListTodo, MessageCircle, MessagesSquare, Monitor,
  Pencil, Plug, Search, Settings, Stamp, User, Users, WandSparkles, Workflow,
} from '@lucide/vue'
import type { Component } from 'vue'

export const lucideIconMap: Record<string, Component> = {
  BadgeCheck, ChartLine, ChevronDown, CircleHelp, Clock, Cpu, Download, EyeOff,
  FolderOpen, Gauge, KeyRound, Library, ListTodo, MessageCircle, MessagesSquare, Monitor,
  Pencil, Plug, Search, Settings, Stamp, User, Users, WandSparkles, Workflow,
}

/** 字符串 → lucide 组件;未登记 key 兜底 CircleHelp,保证配置异常时不白屏 */
export function resolveLucideIcon(name: string): Component {
  return lucideIconMap[name] ?? CircleHelp
}
