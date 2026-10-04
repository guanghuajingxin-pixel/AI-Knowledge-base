# 前端统一开发规范(知识治理专家)

> **效力**:本规范对 Web 端(`web/`)所有新增与修改**强制生效**,与 [docs/ui-guidelines.md](ui-guidelines.md)(UI/UED 设计规范)、[AGENTS.md](../AGENTS.md)(工程与 AI 协作)配合使用;三份文件冲突时以本规范的最新修订为准,并同步回 AGENTS.md 指针。
>
> **技术基线**:Vue 3.4+ `<script setup lang="ts">` · TypeScript strict · Vite 5 · Element Plus 2.x · Pinia · Vue Router 4 · Axios · ECharts 6 · Sass。全站中文界面。

---

## 1. 组件体系

### 1.1 组件库唯一性

- **Element Plus 是唯一 UI 组件库**(当前 `element-plus@^2.7.0`)。新增控件一律优先使用 EP 既有组件(`el-table` / `el-form` / `el-dialog` / `el-tag` 等),**禁止**引入第二套组件库(Ant Design Vue、Naive UI、Arco 等),避免两套设计语言并存。
- 自定义业务组件只做 EP 的补充封装(如 `StatusTag`、`KgPagination`),不做平行替代。

### 1.2 引用方式

- 组件与 API 在 `<script setup>` 中**具名导入**使用:`import { ElMessage, ElMessageBox } from 'element-plus'`;模板内用到的组件显式 import,不依赖隐式全局。
- 不新增任何全局组件/全局图标注册(main.ts 现状保留,见 §2.2)。
- `unplugin-vue-components` 仅用于按需自动解析 EP 组件,不配置自定义 resolver 注册业务组件。

### 1.3 自定义组件目录与命名

- 目录:`src/components/<模块>/`(common / kb / faq / document / library / layout / collection),跨模块复用的展示组件进 `common`。
- 文件 `kebab-case.vue`,组件名 PascalCase;一个文件一个组件。
- 组件必须 `<script setup lang="ts">`,props 用 `defineProps<...>()` 显式类型,事件用 `defineEmits<...>()`,禁止运行时隐式 any。

### 1.4 UI 布局、反馈、表格、弹窗、空态

全部强制遵循 `docs/ui-guidelines.md`(布局稳定防跳动、说明文案 tooltip 化、反馈分层、主按钮唯一、状态色语义、表格列宽规则、空态引导)与 AGENTS.md「表格与弹窗布局要求」。**任何界面改动交付前过其自检清单**(§9)。

### 1.5 列表页统一范式(强制)

> **参考依据**:阿里系 [Ant Design 表格设计规范](https://ant.design/components/table-cn)(列宽策略、固定列、省略与操作列)与 [Ant Design Pro 的 ProTable 列表页范式](https://procomponents.ant.design/components/table)(查询表单 / 工具栏 / 表格 / 分页四段式)。本节将其设计原则映射到 Element Plus + `KgPagination` 的实现——**只取其设计范式,不引入其组件库**(§1.1 禁止第二套组件库)。
> **参照实现**:`governance/collection/KnowledgeCenter.vue`(标准范式)、`knowledge-libraries/DocumentLibraries.vue`(双表格范式)。新增列表页照此结构,不再各自发明。

#### 四段式页面结构

| 段 | 内容 | 关键约束 |
|---|---|---|
| ① 过滤栏 | `el-form inline class="filter-bar"`,左侧筛选控件,右侧按钮组 | 筛选项带 `label` 右对齐;`.filter-actions { margin-left: auto }` 把「查询/重置」推到最右;下拉/日期类变更即查(`@change="search"`),输入类回车或点查询触发;「重置」清空全部条件并回第 1 页 |
| ② 工具栏(可选) | 左侧主操作,右侧次要/搜索 | 主操作按钮唯一且 `type="primary"`(对齐 ui-guidelines 主按钮唯一);无主操作时左搜索右动作亦可,但全站方向一致 |
| ③ 数据表格 | `el-table`,统一属性见下 | 列宽策略与操作列规则见下表 |
| ④ 分页 | 统一 `components/common/KgPagination.vue` | 默认 20 条/页,可选 20/50/100;筛选、搜索、切换库时回第 1 页 |

#### 表格统一属性

- `border`(**强制**):开启 Element Plus 原生**表头列宽拖拽**——表头单元格右缘出现 `col-resize` 手柄,用户可按需调整任意列宽(含固定宽度列)。核心列表页一律开启;列表外边框即 EP 标准样式,不需额外覆盖。
- 行内操作按钮一律 `link type="*" size="small"`(存量页面渐进对齐,新增/改动必须遵守)。
- 操作列 `fixed="right"`,外显按钮 ≤3 个,其余收进「更多」下拉(`placement="bottom-end"`),危险操作归入下拉并二次确认(对齐 AGENTS.md)。
- 表格密度推荐 `size="small"`(数据密集型列表),存量默认尺寸页面渐进对齐,不做一次性全量翻新。

#### 列宽策略

| 列类型 | 宽度 | 附加要求 |
|---|---|---|
| 主内容列(名称/标题) | `min-width` 占据剩余空间 | `show-overflow-tooltip`,悬浮看全名;禁止为其他列挤压主内容列 |
| 状态/开关/数量/时间 | 固定 `width`(按控件实际宽度) | 时间列统一 150–180 |
| 操作列 | 固定 `width`(按钮数 × ~50 + 余量) | 两按钮 140 起步,含 loading 图标余量 |
| 长文本 | 省略 + tooltip | 禁止换行撑高行、禁止缩字号迁就列宽 |

---

## 2. 图标体系(@lucide/vue)

### 2.1 现状基线(2026-09)

| 项 | 现状 | 位置 |
|---|---|---|
| UI 组件库 | Element Plus 2.7,全量注册 | `main.ts` |
| 图标库 | `@element-plus/icons-vue@^2.3.1`,全量全局注册 | `main.ts` |
| 菜单图标 | route meta `icon` 字符串 → namespace import 动态映射 | `router/index.ts` + `Sidebar.vue` |
| 文件类型图标 | 本地 PNG 资源 | `assets/file-icons/` + `utils/file-icon.ts` |

### 2.2 决策

1. **新代码图标统一使用 `@lucide/vue`**(已安装 `@lucide/vue@1.48.0`,官方 Lucide 包,具名导出 PascalCase 组件,ESM tree-shakable)。
2. **禁止对 lucide 做全量/namespace 注册或 `import * as Lucide` 后在模板动态查找**——会破坏 tree-shaking 并把错误推迟到运行时。模板内必须具名导入后直接使用。
3. `@element-plus/icons-vue` **保留但降级**:仅 EP 组件内部自带图标与存量代码使用,随功能改造逐步替换;全量全局注册(现有 main.ts 循环)维持到迁移完成后再移除。
4. 文件类型图标维持 PNG 资产(`file-icon.ts`),品牌 Logo 走站点配置图片,**均不走图标库**。

### 2.3 导入规范(示例)

```vue
<script setup lang="ts">
import { Search, Plus } from '@lucide/vue'
</script>

<template>
  <el-button type="primary" :icon="Plus">新增知识库</el-button>
  <el-icon :size="16"><Search /></el-icon>
</template>
```

- 图标一律包在 `<el-icon>` 内渲染(继承 EP 的尺寸/对齐规范),或作为 `:icon` 传入按钮/输入框前缀。
- 不在 `v-for` / 循环模板里动态拼接组件名;动态场景用 §2.4 注册表。

### 2.4 动态图标注册表(route meta 等字符串→组件)

菜单等需要"配置字符串 → 组件"的场景,统一维护注册表 `web/src/utils/lucide-icons.ts`,**禁止**在模板里用 namespace import 现查:

```ts
import {
  Monitor, MessageCircle, MessagesSquare, Bot, Download, Settings, Gauge,
  Plug, EyeOff, Library, ChartLine, Pencil, Stamp, BadgeCheck, Clock,
  FolderOpen, ListTodo, Cpu, User, Users, WandSparkles, Search, Workflow,
} from '@lucide/vue'
import type { Component } from 'vue'

/** 菜单图标注册表:route meta.icon 只允许出现这里的 key */
export const lucideIconMap = {
  Monitor, MessageCircle, MessagesSquare, Bot, Download, Settings, Gauge,
  Plug, EyeOff, Library, ChartLine, Pencil, Stamp, BadgeCheck, Clock,
  FolderOpen, ListTodo, Cpu, User, Users, WandSparkles, Search, Workflow,
} satisfies Record<string, Component>

export function resolveLucideIcon(name: string): Component {
  return lucideIconMap[name] ?? CircleHelp
}
```

规则:

- `router/index.ts` 的 `meta.icon` 只填注册表中的 key;新增菜单图标**必须同步登记**,并保持 key 与 lucide 导出名一致(PascalCase)。
- `resolveLucideIcon` 兜底 `CircleHelp`,保证后端/配置异常时 UI 不白屏。
- 注册表是**唯一**的"字符串→图标"事实源;Sidebar 迁移见 §2.7。

### 2.5 尺寸、颜色与风格

| 场景 | 尺寸 | 颜色 |
|---|---|---|
| 侧边栏菜单(父级/子级) | 16px | 未激活 `--el-text-color-regular`,激活 `--el-color-primary` |
| 表格操作/行内动作 | 16px | `--el-text-color-regular`;危险操作 `--el-color-danger` |
| 按钮内图标 | 16–18px | 继承按钮文字色(不单独设色) |
| 输入框前缀/搜索 | 16px | `--el-text-color-secondary` |
| 空态/引导大图 | 24–48px | `--el-color-primary`(light 底) |
| 品牌区(Logo 等) | 按布局 | 白色或 `--app-brand-blue` |

- 使用 lucide 默认 `stroke-width`(1.75–2 视觉体系),不逐图标改描边、不加 fill;线性风格全站统一。
- 图标颜色默认继承 `currentColor`;需要语义色时用 §3 Token,禁止硬编码色值。

### 2.6 语义边界

- 纯图标按钮必须带 `el-tooltip`(沿用 ui-guidelines §4);图标不作为唯一可访问标识时给 `aria-label`。
- 不用 emoji / 文字符号替代图标;同义图标全站只保留一个(lucide 已有语义匹配就复用,不新造)。

### 2.7 迁移路径

1. **Phase 1 ✅(已完成 2026-09-30)**:lucide 已装,新页面/新组件直接用 lucide;EP 图标存量不动。
2. **Phase 2 ✅(已完成 2026-09-30)**:`lucide-icons.ts` 注册表已建,router meta.icon 已按 §2.8 映射表切换(22 个 key 全覆盖),Sidebar 动态映射已改走 `resolveLucideIcon`,侧边栏 28 个 lucide 图标已浏览器目检。
3. **Phase 3(待办)**:随组件改造把模板里的 EP 图标换成 lucide;全部替换完成后移除 main.ts 的 EP 图标全局注册循环,并在本文件更新基线。

### 2.8 菜单图标映射表(现有 route meta → lucide)

| 现有(element-plus) | 页面 | lucide 替换 |
|---|---|---|
| `ChatLineRound` | 智能问答 | `MessageCircle` |
| `Monitor` | DEAP 智能问答 | `Monitor` |
| `ChatDotRound` | HiAgent 智能问答 / 问答明细 / 问答库 | `MessagesSquare` |
| `ChatDotSquare` | Dify 智能问答 | `Workflow` |
| `Download` | 知识采集 | `Download` |
| `Setting` | 知识加工 | `Settings` |
| `Odometer` | 解析引擎 | `Gauge` |
| `Connection` | 知识应用 / 知识源管理 | `Plug` |
| `Hide` | 脱敏策略 | `EyeOff` |
| `Collection` | 知识库 | `Library` |
| `DataLine` | 知识运营 / 运营看板 / 知识缺口 | `ChartLine` |
| `EditPen` | 知识纠错 | `Pencil` |
| `Stamp` | 知识治理 / 默认 Logo | `Stamp` |
| `Checked` | 入库审核 | `BadgeCheck` |
| `Clock` | 钉钉知识同步 | `Clock` |
| `Files` | 知识中心 | `FolderOpen` |
| `Tickets` | 同步队列 | `ListTodo` |
| `Cpu` | 系统配置 | `Cpu` |
| `User` | 个人账户 / 用户管理(隐藏) | `User` / `Users` |
| `MagicStick` | 智能体配置 | `WandSparkles` |
| `Search` | 统一检索(隐藏) | `Search` |

> 以上 lucide 图标名均已在本仓库安装包内核验存在;新增页面图标先查 lucide 目录再登记。

---

## 3. 蓝色设计系统

### 3.1 统一决策:品牌蓝 = `#2B6BFF`

- **现状存在两套蓝**:Element Plus 默认主色 `#409EFF`(`global.scss` 已覆盖 `--el-color-primary`),而侧边栏激活态、Logo 渐变已在用 `#2B6BFF`。
- **收敛方案**:品牌主色统一为 **`#2B6BFF`**,将 `--el-color-primary` 及其 light 系列一并覆盖为 `#2B6BFF` 家族(✅ 已完成:global.scss 以 `:root:root` 覆盖,17 个文件 30+ 处硬编码蓝已收敛为 Token,浏览器实测解析值 `#2b6bff`)。
- 若某次改动希望最小影响,可暂用 `#409EFF` 家族,但**不得新增第三种蓝**,并在下个版本随主色迁移收敛。

### 3.2 品牌蓝色阶(自 `#2B6BFF` 计算得出,可复核)

| Token | 值 | 用途 |
|---|---|---|
| `--app-blue-50` | `#EEF3FF` | 选中行/激活底、hover 底 |
| `--app-blue-100` | `#DDE7FF` | 浅描边、Tag light 底 |
| `--app-blue-200` | `#BFD3FF` | 图表辅助蓝 |
| `--app-blue-300` | `#A0BCFF` | 禁用态描边/次要强调 |
| `--app-blue-400` | `#80A6FF` | 图表主序列 |
| `--app-blue-500` | `#6090FF` | hover 强调 |
| **`--app-brand-blue`** | **`#2B6BFF`** | **主色:主按钮、链接、激活态、Logo** |
| `--app-blue-600` | `#2760E6` | 主按钮 hover |
| `--app-blue-700` | `#2256CC` | 主按钮 active/按下 |
| `--app-blue-800` | `#1E4BB3` | 深色区文字、焦点描边 |
| `--app-blue-900` | `#1A4099` | 深色背景(登录页/引导页) |

实现:在 `global.scss` 的 `:root` 中定义上述 `--app-blue-*`,并把 Element Plus 主色覆盖为品牌蓝家族:

```scss
:root:root { /* 双 :root 提升特异性:EP 的 theme-chalk/base.css 可能后于本文件注入,普通 :root 会被压掉 */
  --el-color-primary: #2b6bff;
  --el-color-primary-light-3: #6090ff;
  --el-color-primary-light-5: #80a6ff;
  --el-color-primary-light-7: #bfd3ff;
  --el-color-primary-light-8: #dde7ff;
  --el-color-primary-light-9: #eef3ff;
  --el-color-primary-dark-2: #2256cc;
}
```

### 3.3 语义色(沿用 ui-guidelines §6,不变)

| 语义 | Token | 用途 |
|---|---|---|
| 主操作/链接/选中 | `--el-color-primary`(`#2B6BFF`) | 唯一主按钮、链接、激活态 |
| 成功/完成 | `--el-color-success` `#67c23a` | COMPLETED、连接成功 |
| 处理中/警示 | `--el-color-warning` `#e6a23c` | PARSING/INDEXING、待确认 |
| 危险/失败 | `--el-color-danger` `#f56c6c` | 删除、FAILED |
| 次要信息 | `--el-color-info` `#909399` | 辅助文字、未激活 |

同类状态全站同色同形,**禁止自造颜色**。

### 3.4 文字、背景与边框

| 层级 | Token / 值 | 用途 |
|---|---|---|
| 页面背景 | `#f5f7fa` | 内容区底(global.scss 已定) |
| 卡片/面板 | `#ffffff` | 卡片、弹窗、表格底 |
| 侧边栏底 | `#ffffff` + 右框线 `#e8e8e8` | 现状浅色侧边栏 |
| 主标题 | 16px/600 `--el-text-color-primary`(`#303133`) | 页面/卡片标题 |
| 正文 | 14px `--el-text-color-regular`(`#606266`) | 默认正文 |
| 次要 | 12px `--el-text-color-secondary`(`#909399`) | hint、表头辅助 |
| 边框 | `#dcdfe6` / 分隔 `#e4e7ed` | 控件描边、表格分隔 |

### 3.5 渐变与品牌区

- 渐变(`#2B6BFF → #6D28D9` 蓝紫)仅限**品牌区**:Logo 图标、登录/引导首屏;业务界面禁止渐变,一律纯色 Token。
- 当前 Logo 渐变与侧边栏激活蓝不属同一色系,品牌区允许,但不得扩散到控件。

---

## 4. 页面与路由

- 页面目录 `src/views/<模块>/xxx.vue`,与组件目录模块命名一致。
- 路由 `meta` 约定:`title`(中文,菜单显示)、`icon`(lucide 注册表 key,见 §2.4)、`group`(`feature` / `config` / `admin`)、`parent`(父级路径)、`menuOrder`(同组排序)、`roles`(可见角色)、`hidden`(隐藏菜单)。
- 新增页面必须挂到侧边栏可见路由;隐藏路由(`hidden: true`)仍要填 `title`(用于 Tab 标题)。
- 每个页面职责单一:数据获取放 `src/api/<模块>.ts`,状态进 Pinia store,页面组件只做编排与展示。

---

## 5. TypeScript 与代码风格

- **TS strict 全程开启**(`vue-tsc --noEmit` 门禁)。禁止裸 `any`;确需类型断言的动态场景(如图标注册表)用 `satisfies Record<string, Component>` 并在注释说明。
- props/emits 一律 `defineProps<...>()` / `defineEmits<...>()` 类型写法;`ref` / `computed` 显式泛型。
- 命名:文件 `kebab-case`、组件/类型 PascalCase、函数与变量 camelCase、常量 `UPPER_SNAKE_CASE`、composable 前缀 `use`;布尔变量 `isXxx` / `hasXxx`。
- import 顺序:vue → pinia/router → element-plus → @lucide/vue → 第三方 → `@/` 本地模块。
- 样式:`<style scoped lang="scss">`;颜色/间距一律用 CSS Token(`var(--el-*)` / `var(--app-*)`)或 4/8 栅格,禁止硬编码色值与魔法数字;长列表性能敏感处避免深响应式(必要时 `shallowRef`)。
- 函数风格:早返回、动词开头、单一职责;注释解释"为什么"而非"是什么"。

---

## 6. 工程与质量门禁

| 命令 | 用途 | 门禁 |
|---|---|---|
| `pnpm dev` | 本地开发(HMR) | — |
| `pnpm type-check` | `vue-tsc --noEmit` | **提交前必过** |
| `pnpm test:run` | vitest 单测 | **提交前必过** |
| `pnpm build` | 类型检查 + 产物构建 | **合入前必过** |

- 提交前自检:`pnpm type-check && pnpm test:run && pnpm build` 全绿;UI 改动用 dev server 目检(布局稳定、三态、色语义)。
- 新增依赖必须写明理由,禁止引入未被使用的依赖;图标优先复用 lucide,不新增图标库。
- 产物验证:构建产物在目标路由真实渲染后确认无白屏、无控制台报错。

---

## 7. AI Coding 开发规范

面向 Codex / Cursor / 豆包等 AI 编码工具的协作约定(AGENTS.md 为工程事实源,本文件为前端规范事实源)。

### 7.1 任务指令格式

给 AI 下任务时至少包含:**目标**、**改动范围(文件级)**、**验收标准**、**验证方式**。范围不清时 AI 先问,不擅自扩大改动面。

### 7.2 完成定义(DoD)

- 输入已完整读取(需求、相关组件、ui-guidelines、本规范);约束未越界(只改点名范围,保留未要求内容)。
- 代码通过 `type-check` + `test:run` + `build`;UI 改动通过 ui-guidelines 自检清单并目检。
- 每个事实/数字有来源;交付说明写明验证方式、覆盖范围与剩余缺口。

### 7.3 硬性红线

1. **数据库 schema 变更必须走 alembic 迁移**(改 `kb_common/models.py` 同步补 `alembic/versions/`),禁止 `create_all` / 手工改表。
2. 禁止删除、覆盖、迁移用户数据或生产配置;破坏性操作先说明影响,不在未授权范围执行。
3. 安全:输入输出经 DOMPurify(已有 `marked` + `dompurify` 通道)、API 参数化,不引入 XSS / 注入 / 越权;不改动认证与鉴权逻辑除非任务点名。
4. **禁止臆造 API**:使用 `@lucide/vue`、Element Plus 等依赖前先核对其类型/导出(如 `node_modules/@lucide/vue/dist/lucide-vue.d.ts`),图标名必须核验存在后再写。
5. UI 改动必须符合蓝色 Token(§3)与 ui-guidelines,不引入新色、新组件库、全局注册。
6. 不使用 namespace import 动态找图标、不引入第二套组件库、不改 main.ts 全局注册(迁移除外,见 §2.7)。

### 7.4 验证要求

- 任何改动后先跑最小验证:`pnpm type-check`(必),再按需 `pnpm test:run` / `pnpm build`。
- 样式与布局改动必须在浏览器目检真实渲染(dev server),不能只看代码通过。
- 失败先读报错修正;同一动作失败两次换路径,不静默降级交付。

### 7.5 规范更新

- 本文件为前端唯一事实源;重大变更(图标体系、主色、技术栈)更新后同步 AGENTS.md 指针与 ui-guidelines,保持三处一致。

---

## 8. 交付自检清单(前端)

- [ ] 组件库仅 Element Plus;图标来自 lucide(新代码)且已在注册表登记
- [ ] 颜色全部引用 Token,主色为 `#2B6BFF` 家族;无自造颜色/第三套蓝
- [ ] `pnpm type-check && pnpm test:run && pnpm build` 全绿
- [ ] 布局稳定:切换状态无跳动;反馈载体符合 ui-guidelines 分层
- [ ] 表格列宽:内容列 `min-width`、控件列固定 `width`、长名 `show-overflow-tooltip`
- [ ] 列表页符合 §1.5 范式:四段式结构、表格 `border`(表头可拖拽调列宽)、过滤栏按钮组 `margin-left: auto` 靠右、分页用 `KgPagination`
- [ ] 空态/加载/错误三态齐全,文案具体(含原因与出路)
- [ ] 纯图标按钮有 tooltip;动态图标走注册表
- [ ] 未越界:只改了任务点名的范围;未引入未使用依赖
