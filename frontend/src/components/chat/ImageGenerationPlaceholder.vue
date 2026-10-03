<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

const props = withDefaults(defineProps<{ label?: string; percent?: number; startedAt?: string; ratio?: string }>(), { label: '正在生成图片', ratio: '1.5' })
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 1000) })
onUnmounted(() => { if (timer) clearInterval(timer) })
const elapsed = computed(() => props.startedAt && Number.isFinite(Date.parse(props.startedAt)) ? Math.max(0, Math.floor((now.value - Date.parse(props.startedAt)) / 1000)) : 0)
</script>

<template>
  <div class="chat-image-skeleton generation-particles" :style="{ aspectRatio: ratio }" role="status" :aria-label="label">
    <div class="generation-particle-field" aria-hidden="true"></div>
    <span class="generation-particle-label">{{ label }}</span>
    <div class="generation-particle-progress">
      <small v-if="startedAt">已等待 {{ elapsed }} 秒</small>
      <span v-if="percent !== undefined"><small>阶段进度</small><strong>{{ percent }}%</strong></span>
      <small v-else>等待供应商结果</small>
    </div>
  </div>
</template>

<style scoped>
.generation-particles { position: relative; width: min(420px, 100%); margin: 10px 0; overflow: hidden; background: var(--surface-subtle, #f5f6f8); }
.generation-particle-field { position: absolute; inset: -30%; background-image: radial-gradient(circle, var(--text-muted, #8c95a3) 1px, transparent 1.6px); background-size: 16px 16px; mask-image: radial-gradient(ellipse, #000 15%, transparent 65%); animation: particle-drift 6s ease-in-out infinite alternate; opacity: .45; }
.generation-particle-label { position: absolute; left: 18px; bottom: 22px; color: var(--text-secondary); font-size: 13px; }
.generation-particle-progress { position: absolute; bottom: 18px; right: 18px; display: grid; gap: 5px; text-align: right; color: var(--text-secondary); }
.generation-particle-progress span { display: flex; gap: 8px; align-items: baseline; }
.generation-particle-progress small { font-size: 11px; }
.generation-particle-progress strong { font-size: 22px; font-weight: 500; font-variant-numeric: tabular-nums; }
@keyframes particle-drift { from { transform: translate(-12px, 8px) scale(.95); opacity: .3; } to { transform: translate(12px, -8px) scale(1.12); opacity: .65; } }
@media (prefers-reduced-motion: reduce) { .generation-particle-field { animation: none; } .generation-particles { animation: none; } }
</style>
