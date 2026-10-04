<script setup lang="ts">
import { ref, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
const emit = defineEmits<{ change: [file: File | null, crop: number[] | null] }>()
const input = ref<HTMLInputElement>(), src = ref(''), file = ref<File | null>(null)
const width = ref(0), height = ref(0), crop = ref<number[] | null>(null)
const selection = ref<{ x: number; y: number; w: number; h: number } | null>(null)
let start: { x: number; y: number } | null = null
function choose(event: Event) {
  const selected = (event.target as HTMLInputElement).files?.[0]
  if (!selected) return
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(selected.type) || selected.size > 20*1024*1024) {
    ElMessage.error('请选择 20MB 以内的 JPG、PNG 或 WebP 图片'); return
  }
  if (src.value) URL.revokeObjectURL(src.value)
  file.value = selected; src.value = URL.createObjectURL(selected); crop.value = null; selection.value = null
  emit('change', selected, null)
}
function loaded(event: Event) { const img = event.target as HTMLImageElement; width.value = img.naturalWidth; height.value = img.naturalHeight }
function point(event: PointerEvent) {
  const r = (event.currentTarget as HTMLElement).getBoundingClientRect()
  return { x: Math.max(0, Math.min(1, (event.clientX-r.left)/r.width)), y: Math.max(0, Math.min(1, (event.clientY-r.top)/r.height)) }
}
function down(event: PointerEvent) {
  start = point(event); (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}
function move(event: PointerEvent) {
  if (!start) return
  const p = point(event)
  selection.value = { x: Math.min(start.x, p.x), y: Math.min(start.y, p.y), w: Math.abs(start.x-p.x), h: Math.abs(start.y-p.y) }
}
function up(event: PointerEvent) {
  if (!start) return
  move(event); start = null
  const s = selection.value
  if (!s || s.w*width.value < 16 || s.h*height.value < 16) { reset(); return }
  crop.value = [Math.floor(s.x*width.value), Math.floor(s.y*height.value), Math.floor((s.x+s.w)*width.value), Math.floor((s.y+s.h)*height.value)]
  emit('change', file.value, crop.value)
}
function reset() { selection.value = null; crop.value = null; emit('change', file.value, null) }
onBeforeUnmount(() => { if (src.value) URL.revokeObjectURL(src.value) })
</script>
<template>
  <div class="picker">
    <div class="preview">
      <div v-if="src" class="image-wrap" @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="start = null">
        <img :src="src" alt="待处理物料图片，拖动框选主体" draggable="false" @load="loaded" />
        <div v-if="selection" class="selection" :style="{ left: selection.x*100+'%', top: selection.y*100+'%', width: selection.w*100+'%', height: selection.h*100+'%' }" />
      </div>
      <el-empty v-else description="选择物料照片，可拖动框选主体" :image-size="70" />
    </div>
    <div class="actions">
      <input ref="input" type="file" accept="image/jpeg,image/png,image/webp" hidden @change="choose" />
      <el-button @click="input?.click()">{{ file ? '更换图片' : '选择图片' }}</el-button>
      <el-button :disabled="!crop" @click="reset">清除选框</el-button>
      <el-tooltip content="拖动物料照片框选主体，未框选时使用整图；支持 JPG、PNG、WebP，最大 20MB"><el-tag type="info">{{ crop ? '已框选主体' : '使用整图' }}</el-tag></el-tooltip>
    </div>
  </div>
</template>
<style scoped>
.preview { height: 280px; display: flex; align-items: center; justify-content: center; background: #f5f7fa; border-radius: 6px; overflow: hidden; }
.image-wrap { position: relative; display: inline-flex; max-width: 100%; max-height: 100%; cursor: crosshair; touch-action: none; user-select: none; }
img { max-width: 100%; max-height: 280px; object-fit: contain; pointer-events: none; }
.selection { position: absolute; border: 2px solid var(--el-color-primary); background: color-mix(in srgb, var(--el-color-primary) 13%, transparent); pointer-events: none; box-sizing: border-box; }
.actions { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
.actions .el-button + .el-button { margin-left: 0; }
</style>
