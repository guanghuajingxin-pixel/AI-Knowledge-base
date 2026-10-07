import { defineConfig } from 'vitepress'

export default defineConfig({
  lang: 'zh-CN',
  title: '知识治理 · API 文档',
  description: '平台鉴权、知识库检索与文档解析接入指南',
  base: '/api-guide/',
  outDir: '../public/api-guide',
  cleanUrls: false,
  lastUpdated: true,
  themeConfig: {
    nav: [{ text: '接入指南', link: '/' }, { text: '知识库检索', link: '/retrieval' }, { text: '返回平台', link: '../settings/api-keys/', target: '_self' }],
    sidebar: [
      { text: '开始接入', items: [{ text: '概览', link: '/' }, { text: '鉴权与 API Key', link: '/authentication' }] },
      { text: 'API 参考', items: [{ text: '知识库检索', link: '/retrieval' }, { text: '文档解析', link: '/parser' }] },
      { text: '部署与维护', items: [{ text: '身份中心接入', link: '/administration' }] },
    ],
    search: { provider: 'local', options: { locales: { root: { translations: {
      button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
      modal: { noResultsText: '没有找到相关内容', resetButtonTitle: '清空搜索', footer: { selectText: '选择', navigateText: '切换', closeText: '关闭' } },
    } } } } },
    outline: { label: '本页目录', level: [2, 3] },
    docFooter: { prev: '上一页', next: '下一页' },
    darkModeSwitchLabel: '切换主题', sidebarMenuLabel: '目录', returnToTopLabel: '返回顶部',
    footer: { message: '知识治理专家 · 开发者文档' },
  },
})
