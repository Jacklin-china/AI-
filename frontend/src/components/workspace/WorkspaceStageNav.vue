<script setup lang="ts">
import { AlertCircle, Check, Circle, Loader2, Minus } from 'lucide-vue-next'

/** 顶部轻量阶段导航：把 Workflow 从「常驻一列」变成「可点击的导航条」。 */
import type { StageItem } from './stageTypes'

defineProps<{ stages: StageItem[]; activeId?: string }>()
</script>

<template>
  <ol v-if="stages.length" class="stage-nav" aria-label="工作流阶段进度">
    <li
      v-for="stage in stages"
      :key="stage.id"
      class="stage-chip"
      :class="{ active: stage.id === activeId }"
      :data-status="stage.status"
      :aria-current="stage.id === activeId ? 'step' : undefined"
      :title="stage.detail ? `${stage.label} · ${stage.detail}` : stage.label"
    >
      <Check v-if="stage.status === 'completed'" :size="12" :stroke-width="2.6" />
      <Loader2 v-else-if="stage.status === 'running'" :size="12" :stroke-width="2.4" class="stage-spin" />
      <AlertCircle v-else-if="stage.status === 'failed'" :size="12" :stroke-width="2.4" />
      <Minus v-else-if="stage.status === 'waiting'" :size="12" :stroke-width="2.4" />
      <Circle v-else :size="8" :stroke-width="2.4" />
      <span>{{ stage.label }}</span>
    </li>
  </ol>
</template>

<style scoped>
.stage-nav {
  display: flex; align-items: center; gap:8px;
  flex-shrink: 0; width: 100%;
  padding:8px 24px 8px;
  overflow-x: auto; scrollbar-width: none;
}
.stage-nav::-webkit-scrollbar { display: none; }
.stage-chip {
  display: inline-flex; align-items: center; gap:8px; flex-shrink: 0;
  min-height: 28px; padding:0 12px;
  border: 1px solid transparent; border-radius: 999px;
  background: transparent; color: var(--text-muted);
  font-size: 12px; white-space: nowrap; list-style: none;
}
.stage-chip.active { background: var(--sidebar-selected); color: var(--sidebar-ink); border-color: var(--border-muted); }
.stage-chip svg { flex-shrink: 0; }
.stage-chip[data-status="completed"] { color: var(--success); }
.stage-chip[data-status="running"] { color: var(--info); }
.stage-chip[data-status="failed"] { color: var(--danger); }
.stage-chip[data-status="waiting"] { color: var(--warning); }
.stage-chip.active[data-status], .stage-chip.active { color: var(--sidebar-ink); }
.stage-spin { animation: stage-rotate 1.1s linear infinite; }
@keyframes stage-rotate { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .stage-spin { animation: none; } }
</style>
