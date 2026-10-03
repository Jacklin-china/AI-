<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, type Component } from 'vue'
import { PanelLeft, Plus } from 'lucide-vue-next'

defineProps<{
  navigation: { id: string; label: string; icon: Component }[]
  activePage: string
  navigationOpen: boolean
  creatingConversation: boolean
}>()
const emit = defineEmits<{
  'update:navigationOpen': [open: boolean]
  selectPage: [id: string]
  newConversation: []
}>()
const composerHost = ref<HTMLElement | null>(null)
const composerSpace = ref(220)
let observer: ResizeObserver | undefined
onMounted(() => {
  // Keep the last message reachable even when the existing composer grows.
  observer = new ResizeObserver(() => {
    composerSpace.value = Math.ceil(composerHost.value?.getBoundingClientRect().height ?? 184) + 36
  })
  if (composerHost.value) observer.observe(composerHost.value)
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <section class="comic-shell" aria-label="AI 导演工作台" :style="{ '--comic-composer-space': `${composerSpace}px` }">
    <aside v-show="navigationOpen" class="comic-shell-sidebar" aria-label="Comic 工作区侧栏">
      <div class="comic-shell-identity"><strong>Comic Workspace</strong><span>漫剧创作</span></div>
      <button class="comic-shell-new" :disabled="creatingConversation" @click="emit('newConversation')"><Plus :size="17" /> 新建对话</button>
      <nav class="comic-shell-pages" aria-label="创作导航">
        <span class="comic-shell-label">工作区</span>
        <button v-for="item in navigation" :key="item.id" :aria-label="item.label" :aria-current="activePage === item.id ? 'page' : undefined" @click="emit('selectPage', item.id)">
          <component :is="item.icon" :size="17" /><span>{{ item.label }}</span>
        </button>
      </nav>
      <section class="comic-shell-recent" aria-label="最近对话">
        <span class="comic-shell-label">最近对话</span>
        <slot name="recent" />
      </section>
    </aside>
    <div class="comic-shell-main">
      <div class="comic-shell-top">
        <button class="comic-shell-toggle" :aria-expanded="navigationOpen" :aria-label="navigationOpen ? '收起导航' : '展开导航'" @click="emit('update:navigationOpen', !navigationOpen)"><PanelLeft :size="18" /></button>
        <slot name="toolbar" />
      </div>
      <div class="comic-shell-heading"><slot name="heading" /></div>
      <div class="comic-shell-content"><slot /></div>
      <div ref="composerHost" class="comic-shell-composer" aria-label="统一创作输入"><slot name="composer" /></div>
    </div>
  </section>
</template>

<style scoped>
.comic-shell { display:flex; height:100%; min-height:0; min-width:0; color:var(--text-primary); background:var(--surface); }
.comic-shell-sidebar { display:flex; flex-direction:column; flex:0 0 228px; min-height:0; min-width:0; padding:18px 12px 12px; background:var(--surface-subtle); }
.comic-shell-identity { display:grid; gap:3px; padding:0 10px 16px; }
.comic-shell-identity strong { font-size:13px; font-weight:600; }
.comic-shell-identity span { font-size:11px; color:var(--text-muted); }
.comic-shell-new, .comic-shell-pages button { display:flex; align-items:center; gap:10px; min-height:36px; padding:8px 10px; border:0; border-radius:8px; background:transparent; color:var(--text-primary); text-align:left; font:inherit; font-size:13px; cursor:pointer; }
.comic-shell-new { flex-shrink:0; margin-bottom:20px; }
.comic-shell-pages { display:grid; gap:2px; flex-shrink:0; }
.comic-shell-label { display:block; padding:6px 10px 10px; font-size:11px; color:var(--text-muted); }
.comic-shell-pages button svg { color:var(--text-secondary); }
.comic-shell-pages button[aria-current] { background:var(--sidebar-selected); }
.comic-shell-new:hover, .comic-shell-pages button:hover { background:var(--sidebar-hover); }
.comic-shell-new:disabled { opacity:.45; cursor:not-allowed; }
.comic-shell-recent { display:flex; flex-direction:column; flex:1; min-height:0; padding-top:24px; }
.comic-shell-main { position:relative; display:flex; flex:1; flex-direction:column; min-width:0; min-height:0; }
.comic-shell-top { display:flex; align-items:center; flex-shrink:0; gap:12px; height:60px; padding:10px 24px; background:transparent; }
.comic-shell-toggle { display:grid; place-items:center; flex:0 0 30px; width:30px; height:32px; border:0; border-radius:7px; background:transparent; color:var(--text-secondary); cursor:pointer; }
.comic-shell-toggle:hover { background:var(--surface-subtle); }
.comic-shell-heading { display:flex; align-items:center; flex-shrink:0; gap:12px; height:62px; padding:8px 32px 16px; background:transparent; }
.comic-shell-content { display:flex; flex:1; min-height:0; min-width:0; }
.comic-shell-composer { position:absolute; z-index:5; bottom:14px; left:50%; transform:translateX(-50%); width:min(760px, calc(100% - 48px)); background:transparent; }
.comic-shell-content :deep(.stage-scroll), .comic-shell-content :deep(.workspace-messages) {
  padding-bottom:var(--comic-composer-space);
  scroll-padding-bottom:var(--comic-composer-space);
  mask-image:linear-gradient(to bottom, black calc(100% - var(--comic-composer-space)), transparent calc(100% - var(--comic-composer-space) + 16px));
}
:deep(.message-composer) { border-radius:18px; box-shadow:var(--shadow-card); }
:deep(.message-composer footer > span) { min-width:0; overflow-wrap:anywhere; }
button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
@media(max-width:1000px) { .comic-shell-sidebar { flex-basis:204px; } .comic-shell-top { padding-inline:16px; gap:8px; } .comic-shell-heading { padding-inline:24px; } }
@media(max-width:600px) { .comic-shell-sidebar { flex-basis:180px; padding-inline:8px; } .comic-shell-top { padding-inline:10px; gap:6px; } .comic-shell-heading { padding:8px 16px 12px; } .comic-shell-composer { bottom:10px; width:calc(100% - 24px); } }
@media(max-width:480px) {
  .comic-shell-sidebar { flex-basis:160px; }
  .comic-shell-top { flex-wrap:wrap; height:92px; padding:10px 8px; }
  .comic-shell-top :deep(.toolbar-mode) { flex-basis:100%; min-width:0; }
  .comic-shell-top :deep(select) { max-width:100%; }
  .comic-shell-heading :deep(small) { display:none; }
}
</style>
