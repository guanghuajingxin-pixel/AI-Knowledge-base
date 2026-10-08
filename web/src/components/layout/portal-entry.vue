<script setup lang="ts">
import { ElTooltip } from 'element-plus'
import portalMark from '@/assets/portal-mark.svg'

const defaultPortalUrl = new URL(window.location.origin)
defaultPortalUrl.port = '3200'
const portalUrl = import.meta.env.VITE_PORTAL_URL?.trim() || defaultPortalUrl.href
</script>

<template>
  <ElTooltip content="打开杰克科技 AIGC 门户（新标签页）" placement="bottom">
    <a
      class="portal-entry"
      :href="portalUrl"
      target="_blank"
      rel="noopener noreferrer"
      aria-label="打开杰克科技 AIGC 门户（新标签页）"
    >
      <img :src="portalMark" class="portal-mark" alt="" width="28" height="28" />
      <span class="portal-label">AIGC 门户</span>
    </a>
  </ElTooltip>
</template>

<style scoped lang="scss">
.portal-entry {
  display: inline-flex;
  align-items: center;
  align-self: center;
  flex-shrink: 0;
  gap: 8px;
  height: 32px;
  padding: 0 8px 0 4px;
  border-radius: 8px;
  color: var(--el-color-primary);
  text-decoration: none;
  white-space: nowrap;
  transition: background-color 0.15s ease;

  &:hover {
    background: var(--el-color-primary-light-9);
  }

  &:focus-visible {
    outline: 2px solid var(--el-color-primary);
    outline-offset: 2px;
  }
}

.portal-mark {
  display: block;
  flex-shrink: 0;
}

.portal-label {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.4px;
}

@media (max-width: 640px) {
  .portal-entry { padding: 0 4px; }
  .portal-label { display: none; }
}
</style>
