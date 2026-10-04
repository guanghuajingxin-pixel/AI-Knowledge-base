import { createApp } from 'vue'
import 'element-plus/dist/index.css'
import './styles/theme.scss'
import App from './App.vue'
import { initializeAuth } from './auth/session'

void initializeAuth().then(() => createApp(App).mount('#app'))
