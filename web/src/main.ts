import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import App from './App.vue'
import { initializeAuth, sso, authError } from './auth/session'
import { useUserStore } from './stores/user'
import './styles/global.scss'
import { setupMock } from './mocks'

async function bootstrap() {
  await setupMock()
  const app = createApp(App)
  for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
    app.component(key, component)
  }
  const pinia = createPinia()
  app.use(pinia)
  await initializeAuth()
  const userStore = useUserStore(pinia)
  if (sso.isSso) {
    userStore.logout(false)
    sso.subscribe(token => {
      if (token) userStore.setToken(token)
      else userStore.logout(false)
    })
    if (userStore.token) {
      try { await userStore.fetchUserInfo() }
      catch (error) {
        sso.clear(); userStore.logout(false)
        const failure = error as { response?: { data?: { detail?: string } } }
        authError.value = failure.response?.data?.detail || '统一账号尚未获得访问权限，请联系管理员。'
      }
    }
  }
  const { default: router } = await import('./router')
  app.use(router)
  app.use(ElementPlus, { locale: zhCn })
  app.mount('#app')
}

bootstrap()
