import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ElementPlus from 'element-plus'
import Page from '../api-keys.vue'
import { createPlatformKey, listPlatformKeys } from '@/api/platform-keys'

vi.mock('@/api/platform-keys', () => ({
  listPlatformKeys: vi.fn(), createPlatformKey: vi.fn(),
  updatePlatformKey: vi.fn(), deletePlatformKey: vi.fn(),
}))
vi.mock('@/utils/api-docs', () => ({ openApiGuide: vi.fn() }))

beforeEach(() => {
  vi.mocked(listPlatformKeys).mockResolvedValue([])
  vi.mocked(createPlatformKey).mockResolvedValue({ id: 'test', name: 'test', client_id: 'test',
    enabled: true, created_at: null, permissions: ['knowledge:retrieve'], raw_key: 'test:only-once' })
})
afterEach(() => { document.body.innerHTML = ''; vi.clearAllMocks() })

function mountPage() {
  return mount(Page, { attachTo: document.body, global: { plugins: [ElementPlus], stubs: { transition: false } } })
}

describe('平台 API Key 页面', () => {
  it('空列表提供签发入口，签发后的密钥关闭即清空', async () => {
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('尚无 API Key')
    await wrapper.findAll('button').find(b => b.text() === '签发 Key')!.trigger('click')
    await flushPromises()
    await wrapper.find('input[placeholder="例如：售后服务系统"]').setValue('售后应用')
    const buttons = wrapper.findAll('button').filter(b => b.text() === '签发 Key')
    await buttons[buttons.length - 1].trigger('click')
    await flushPromises()
    expect(createPlatformKey).toHaveBeenCalledWith('售后应用')
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('test:only-once')
    await wrapper.findAll('button').find(b => b.text() === '已保存，关闭')!.trigger('click')
    await flushPromises()
    const textarea = wrapper.find('textarea')
    expect(textarea.exists() ? (textarea.element as HTMLTextAreaElement).value : '').toBe('')
    wrapper.unmount()
  })

  it('加载失败展示配置原因并禁止签发', async () => {
    vi.mocked(listPlatformKeys).mockRejectedValue({ response: { data: { detail: '未配置平台密钥管理服务账号' } } })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('未配置平台密钥管理服务账号')
    expect(wrapper.findAll('button').find(b => b.text() === '签发 Key')!.attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
})
