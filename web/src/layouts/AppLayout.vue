<script setup lang="ts">
import Sidebar from '@/components/layout/Sidebar.vue'
import TabBar from '@/components/layout/TabBar.vue'
import { useTabsStore } from '@/stores/tabs'

const tabsStore = useTabsStore()
</script>

<template>
  <div class="app-layout">
    <Sidebar />
    <div class="main-section">
      <TabBar />
      <div class="content-area">
        <!-- KeepAlive：页签切换不销毁页面组件，进行中的检索/流式请求与页面状态跨页签存活；
             include 由页签 store 提供（页签关闭即释放缓存），key 取路径保证同组件多开（如检索测试-1/-2）各自独立缓存 -->
        <router-view v-slot="{ Component, route: view }">
          <keep-alive :include="tabsStore.cacheNames">
            <component :is="Component" :key="view.path" />
          </keep-alive>
        </router-view>
      </div>
    </div>
  </div>
</template>

<style scoped>
.app-layout {
  display: flex;
  height: 100vh;
}
.main-section {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.content-area {
  flex: 1;
  min-height: 0;
  /* 滚动权下放给页面内部（页签 pane / page-content），外框只负责按比例分配高度 */
  overflow: hidden;
  background: #f5f7fa;
}
</style>
