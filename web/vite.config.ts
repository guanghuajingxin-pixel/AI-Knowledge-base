/// <reference types="vitest" />
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [
    {
      name: 'api-guide-index',
      configureServer(server) {
        server.middlewares.use((req, _res, next) => {
          if (req.url === '/api-guide/' || req.url === '/api-guide') req.url = '/api-guide/index.html'
          next()
        })
      },
    },
    vue(),
    AutoImport({
      resolvers: [ElementPlusResolver()],
      imports: ['vue', 'vue-router', 'pinia'],
      dts: 'src/auto-imports.d.ts',
    }),
    Components({
      resolvers: [ElementPlusResolver()],
      dts: 'src/components.d.ts',
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 3000,
    host: true,
    proxy: {
      '/api/openapi': { target: process.env.KB_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: true },
      '/api/v1/faq': { target: process.env.FAQ_API_TARGET || 'http://127.0.0.1:8004', changeOrigin: true },
      // timeout/proxyTimeout=0：显式禁用代理层超时，SSE 流式问答长连接不被掐断
      '/api/v1': { target: process.env.KB_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: true, timeout: 0, proxyTimeout: 0 },
    },
  },
  css: {
    preprocessorOptions: {
      scss: {
        additionalData: `@use "@/styles/variables.scss" as *;`,
      },
    },
  },
  test: {
    environment: 'jsdom',
    server: { deps: { inline: ['element-plus'] } },
    globals: true,
  },
})
