/// <reference types="vitest" />
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [vue()],
    server: {
      port: 3200,
      proxy: { '/api/v1': { target: env.PORTAL_API_TARGET || 'http://10.10.166.2:8080', changeOrigin: true, timeout: 0, proxyTimeout: 0 } },
    },
    test: { environment: 'node', include: ['tests/**/*.test.ts'] },
  }
})
