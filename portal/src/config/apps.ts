export const applications = [
  { id: 'knowledge', name: '知识治理专家', caption: '让企业知识成为生产力', description: '汇聚企业知识，检索可信答案', icon: 'BookOpen', url: import.meta.env.VITE_KNOWLEDGE_URL || 'http://10.10.166.2:8080/chat' },
  { id: 'evaluation', name: '智能体测评', caption: '让每一次回答更可靠', description: '管理测评数据，评估智能体表现', icon: 'FlaskConical', url: import.meta.env.VITE_EVALUATION_URL || 'http://10.10.166.2:8082/datasets' },
  { id: 'skillhub', name: 'SkillHub', caption: '让好用的能力随手可得', description: '发现、复用与共享智能体技能', icon: 'Blocks', url: import.meta.env.VITE_SKILLHUB_URL || 'http://10.10.166.2:3100/' },
] as const
export type AppId = typeof applications[number]['id']
export type PageId = 'home' | AppId
export function pageFromHash(hash: string): PageId {
  const id = hash.replace(/^#\/?/, '')
  return applications.some(app => app.id === id) ? id as AppId : 'home'
}
