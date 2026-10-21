<script setup lang="ts">
import { onBeforeUnmount, watch } from 'vue'
import { X } from 'lucide-vue-next'

/** Inspector 不再常驻占列：按需从右侧滑出，关闭后主区恢复完整宽度。 */
const props = defineProps<{ open: boolean; title?: string }>()
const emit = defineEmits<{ close: [] }>()

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape' && props.open) emit('close')
}
watch(() => props.open, (open) => {
  document.body.style.overflow = open ? 'hidden' : ''
  if (open) window.addEventListener('keydown', onKey)
  else window.removeEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  document.body.style.overflow = ''
  window.removeEventListener('keydown', onKey)
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="inspector-drawer-root">
      <div class="inspector-scrim" @click="emit('close')" />
      <aside class="inspector-drawer" role="dialog" aria-modal="true" :aria-label="title ?? '任务详情'">
        <header class="inspector-drawer-head">
          <strong>{{ title ?? '任务详情' }}</strong>
          <button class="inspector-drawer-close" aria-label="关闭详情" @click="emit('close')"><X :size="16" /></button>
        </header>
        <div class="inspector-drawer-body"><slot /></div>
      </aside>
    </div>
  </Teleport>
</template>

<style scoped>
.inspector-drawer-root { position: fixed; inset: 0; z-index: 90; }
.inspector-scrim { position: absolute; inset: 0; background: rgb(23 26 31 / .28); animation: inspector-fade .16s ease; }
.inspector-drawer {
  position: absolute; top: 0; right: 0; bottom: 0;
  display: flex; flex-direction: column;
  width: min(400px, 92vw);
  background: var(--surface); border-left: 1px solid var(--border-muted);
  box-shadow: -12px 0 32px rgb(23 26 31 / .10);
  animation: inspector-slide .2s cubic-bezier(.22,.61,.36,1);
}
.inspector-drawer-head {
  display: flex; align-items: center; justify-content: space-between; gap:12px;
  flex-shrink: 0; height: 56px; padding:0 20px; border-bottom: 1px solid var(--border-muted);
}
.inspector-drawer-head strong { font-size: 13.5px; font-weight: 600; }
.inspector-drawer-close {
  display: grid; place-items: center; width: 28px; height: 28px;
  border: 0; border-radius: 8px; background: transparent; color: var(--text-secondary); cursor: pointer;
}
.inspector-drawer-close:hover { background: var(--surface-subtle); color: var(--text-primary); }
.inspector-drawer-body { flex: 1; min-height: 0; overflow: auto; padding:16px 20px 24px; }
@keyframes inspector-slide { from { transform: translateX(16px); opacity: .4; } to { transform: none; opacity: 1; } }
@keyframes inspector-fade { from { opacity: 0; } to { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .inspector-drawer, .inspector-scrim { animation: none; } }
</style>
