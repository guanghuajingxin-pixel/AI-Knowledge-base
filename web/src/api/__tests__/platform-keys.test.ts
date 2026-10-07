import { afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { InternalAxiosRequestConfig } from 'axios'
import request from '../request'
import { createPlatformKey, deletePlatformKey, listPlatformKeys, updatePlatformKey } from '../platform-keys'

const originalAdapter = request.defaults.adapter
const originalBase = request.defaults.baseURL
afterEach(() => { request.defaults.adapter = originalAdapter; request.defaults.baseURL = originalBase })

it('所有 Key 操作使用平台 API 前缀一次，保留统一请求拦截器', async () => {
  setActivePinia(createPinia())
  request.defaults.baseURL = '/api/v1'
  const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => ({ data: [], status: 200, statusText: 'OK', headers: {}, config }))
  request.defaults.adapter = adapter
  await listPlatformKeys()
  await createPlatformKey('test')
  await updatePlatformKey('id', { enabled: false })
  await deletePlatformKey('id')
  expect(adapter.mock.calls.map(([config]) => request.getUri(config))).toEqual([
    '/api/v1/platform/api-keys', '/api/v1/platform/api-keys',
    '/api/v1/platform/api-keys/id', '/api/v1/platform/api-keys/id',
  ])
})
