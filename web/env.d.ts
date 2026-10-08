/// <reference types="vite/client" />
/// <reference types="vue-router" />

import 'vue-router'

declare module 'vue-router' {
  interface RouteMeta {
    title?: string
    icon?: string
    hidden?: boolean
    public?: boolean
    roles?: string[]
  }
}

interface ImportMetaEnv {
  readonly VITE_PORTAL_URL?: string
  readonly VITE_USE_MOCK: string
  readonly VITE_API_BASE_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}
