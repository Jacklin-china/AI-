<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { fittedImage, boundImagePan, zoomImage } from './imageViewport'
const props = defineProps<{ src: string; alt: string }>()
const emit = defineEmits<{ error: []; empty: [] }>()
const stage = ref<HTMLElement | null>(null), size = ref({ width: 0, height: 0 }), natural = ref({ width: 0, height: 0 })
const zoom = ref(1), pan = ref({ x: 0, y: 0 })
const fit = computed(() => fittedImage(natural.value, size.value))
const frame = computed(() => ({ width: fit.value.width + 'px', height: fit.value.height + 'px' }))
const surface = computed(() => ({ width: fit.value.width * zoom.value + 'px', height: fit.value.height * zoom.value + 'px',
  left: (fit.value.width * (1 - zoom.value) / 2 + pan.value.x) + 'px', top: (fit.value.height * (1 - zoom.value) / 2 + pan.value.y) + 'px' }))
let observer: ResizeObserver | undefined
let drag: { id: number; x: number; y: number; pan: { x: number; y: number } } | null = null
function reset() { zoom.value = 1; pan.value = { x: 0, y: 0 } }
function measure() {
  const rect = stage.value?.getBoundingClientRect()
  if (rect) { size.value = { width: rect.width, height: rect.height }; pan.value = boundImagePan(pan.value, fit.value, zoom.value) }
}
function loaded(event: Event) { const img = event.target as HTMLImageElement; natural.value = { width: img.naturalWidth, height: img.naturalHeight }; measure() }
function scale(multiplier: number, anchor = { x: 0, y: 0 }) { const view = zoomImage(pan.value, zoom.value, multiplier, anchor, fit.value); zoom.value = view.zoom; pan.value = view.pan }
function wheel(event: WheelEvent) {
  const rect = stage.value?.getBoundingClientRect(); if (!rect) return
  const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.height : 1)
  scale(Math.exp(-delta * .0015), { x: event.clientX - rect.left - rect.width / 2, y: event.clientY - rect.top - rect.height / 2 })
}
function down(event: PointerEvent) {
  if (event.button !== 0 || zoom.value <= 1) return
  drag = { id: event.pointerId, x: event.clientX, y: event.clientY, pan: { ...pan.value } }
  stage.value?.setPointerCapture(event.pointerId); event.preventDefault()
}
function move(event: PointerEvent) { if (drag?.id === event.pointerId) pan.value = boundImagePan({ x: drag.pan.x + event.clientX - drag.x, y: drag.pan.y + event.clientY - drag.y }, fit.value, zoom.value) }
function up(event: PointerEvent) { if (drag?.id === event.pointerId) { drag = null; stage.value?.releasePointerCapture(event.pointerId) } }
watch(() => props.src, () => { reset(); natural.value = { width: 0, height: 0 } })
onMounted(() => { observer = new ResizeObserver(measure); if (stage.value) observer.observe(stage.value); measure() })
onBeforeUnmount(() => observer?.disconnect())
</script>
<template>
  <div ref="stage" class="image-viewer" @wheel.prevent="wheel" @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="up" @click.self="zoom <= 1 && emit('empty')">
    <div class="image-viewer-frame" :style="frame"><img :src="src" :alt="alt" :style="surface" draggable="false" @load="loaded" @error="emit('error')" /></div>
    <div class="image-viewer-tools" @pointerdown.stop><button class="ui-button quiet sm" aria-label="缩小图片" @click="scale(1 / 1.2)">−</button><button class="ui-button quiet sm" @click="reset">{{ Math.round(zoom * 100) }}%</button><button class="ui-button quiet sm" aria-label="放大图片" @click="scale(1.2)">＋</button><button class="ui-button quiet sm" @click="reset">适应画布</button></div>
  </div>
</template>
<style scoped>
.image-viewer { position:relative; display:grid; place-items:center; width:100%; height:100%; min-height:180px; overflow:hidden; touch-action:none; }
.image-viewer-frame { position:relative; overflow:hidden; border-radius:var(--radius-sm); }
.image-viewer-frame img { position:absolute; max-width:none; user-select:none; }
.image-viewer-tools { position:absolute; bottom:12px; display:flex; align-items:center; border-radius:var(--radius-control); background:var(--surface); box-shadow:var(--shadow-sm); }
</style>
