import axios, { type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { sso } from '@/auth/session'

const service: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL,
  // 全局 60s（原 30s 偏短：大列表/慢接口易超时）；个别长任务在各自 api 模块单独放宽
  timeout: 60000,
})

service.interceptors.request.use(
  async (config: InternalAxiosRequestConfig) => {
    const userStore = useUserStore()
    if (sso.isSso) userStore.setToken(await sso.getToken())
    if (userStore.token) {
      config.headers.Authorization = `Bearer ${userStore.token}`
    }
    return config
  },
  (error) => Promise.reject(error),
)

service.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const detail = error.response?.data?.detail
    const message = (typeof detail === 'string' ? detail : '') || error.response?.data?.message || error.message || '请求失败'
    if (error.response?.status === 401) {
      // 登录/免登接口 401（凭证错误）：不登出/不重载，交由调用方 catch 提示或回退
      const reqUrl = error.config?.url || ''
      if (reqUrl.includes('/auth/login') || reqUrl.includes('/auth/dingtalk-login')) {
        return Promise.reject(error)
      }
      const userStore = useUserStore()
      userStore.logout(false)
      // A rejected SSO profile during bootstrap must render the login error, not loop redirects.
      if (!(sso.isSso && reqUrl.includes('/users/me'))) window.location.href = '/login'
    } else {
      ElMessage.error(message)
    }
    return Promise.reject(error)
  },
)

export default service
