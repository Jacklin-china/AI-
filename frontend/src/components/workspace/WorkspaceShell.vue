<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, type Component } from 'vue'
import { PanelLeft, PanelLeftClose, PanelLeftOpen, Plus } from 'lucide-vue-next'

defineProps<{
  navigation: { id: string; label: string; icon: Component }[]
  activePage: string
  navigationOpen: boolean
  creatingConversation: boolean
  /** 桌面端「图标条」折叠态：与 navigationOpen（抽屉显隐）语义不同，两者互不影响。 */
  navigationCollapsed?: boolean
  identity?: string
  subtitle?: string
  hideHeading?: boolean
  workspaceLabel?: string
  newConversationLabel?: string
  /** 首页形态之一：整块不渲染二级侧栏，宽度全部让给对话区。
      默认 false —— 工作区/任务中心/资产/设置依赖这层侧栏做页面切换。 */
  hideNavigation?: boolean
  /** 首页形态之二：内容与输入框同处一个滚动区（输入框不再浮在底部）。
      默认 false —— 工作区需要输入框常驻可见，便于随时补充指令。 */
  flowComposer?: boolean
}>()
const emit = defineEmits<{
  'update:navigationOpen': [open: boolean]
  'update:navigationCollapsed': [collapsed: boolean]
  selectPage: [id: string]
  newConversation: []
}>()
const composerHost = ref<HTMLElement | null>(null)
const composerSpace = ref(220)
let observer: ResizeObserver | undefined
onMounted(() => {
  // Keep the last message reachable even when the existing composer grows.
  observer = new ResizeObserver(() => {
    if (!composerHost.value) { composerSpace.value = 0; return }
    composerSpace.value = Math.ceil(composerHost.value.getBoundingClientRect().height) + 36
  })
  if (composerHost.value) observer.observe(composerHost.value)
  else composerSpace.value = 0
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <section class="ws-shell" :aria-label="workspaceLabel ?? 'AI 导演工作台'" :style="{ '--ws-composer-space': `${composerSpace}px` }">
    <aside v-if="!hideNavigation" v-show="navigationOpen" class="ws-shell-sidebar" :class="{ 'is-collapsed': navigationCollapsed }" :aria-label="navigationCollapsed ? '会话工作区图标侧栏' : '会话工作区侧栏'">
      <div class="ws-shell-identity"><strong>{{ identity ?? 'Kantoku' }}</strong><span v-if="subtitle">{{ subtitle }}</span></div>
      <button v-if="newConversationLabel" class="ws-shell-new" :disabled="creatingConversation" @click="emit('newConversation')"><Plus :size="17" /> <span>{{ newConversationLabel }}</span></button>
      <nav class="ws-shell-pages" aria-label="创作导航">
        <span class="ws-shell-label">工作区</span>
        <button v-for="item in navigation" :key="item.id" :aria-label="item.label" :aria-current="activePage === item.id ? 'page' : undefined" @click="emit('selectPage', item.id)">
          <component :is="item.icon" :size="17" /><span>{{ item.label }}</span>
        </button>
      </nav>
      <section v-if="$slots.recent" class="ws-shell-recent" aria-label="最近对话">
        <span class="ws-shell-label">最近对话</span>
        <slot name="recent" />
      </section>
    </aside>
    <div class="ws-shell-main">
      <div class="ws-shell-top">
        <button v-if="!hideNavigation" class="ws-shell-toggle" :aria-expanded="navigationOpen" :aria-label="navigationOpen ? '收起导航' : '展开导航'" @click="emit('update:navigationOpen', !navigationOpen)"><PanelLeft :size="18" /></button>
        <button v-if="!hideNavigation && navigationCollapsed !== undefined" class="ws-shell-collapse" :aria-expanded="!navigationCollapsed" :aria-label="navigationCollapsed ? '展开侧栏' : '折叠侧栏'" @click="emit('update:navigationCollapsed', !navigationCollapsed)"><PanelLeftOpen v-if="navigationCollapsed" :size="17" /><PanelLeftClose v-else :size="17" /></button>
        <slot name="toolbar" />
      </div>
      <div v-if="$slots.stage" class="ws-shell-stage"><slot name="stage" /></div>
      <div v-if="!hideHeading" class="ws-shell-heading"><slot name="heading" /></div>
      <div class="ws-shell-body" :class="{ 'is-flow': flowComposer }">
        <div class="ws-shell-content"><slot /></div>
        <div v-if="$slots.composer" ref="composerHost" class="ws-shell-composer" aria-label="统一创作输入"><slot name="composer" /></div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.ws-shell { display:flex; height:100%; min-height:0; min-width:0; color:var(--text-primary); background:var(--surface); }
.ws-shell-sidebar { display:flex; flex-direction:column; flex:0 0 var(--sidebar-width); min-height:0; min-width:0; padding:var(--space-5) var(--space-3) var(--space-3); background:var(--surface-subtle); border-right:1px solid var(--border-muted); overflow:hidden; transition:flex-basis var(--duration-med) var(--ease-out), padding var(--duration-med) var(--ease-out); }
/* 折叠（图标条）：宽度过渡是丝滑感的来源，同时收窄内边距保持图标居中。 */
.ws-shell-sidebar.is-collapsed { flex-basis:var(--sidebar-collapsed); padding-inline:var(--space-2); }
.ws-shell-sidebar.is-collapsed .ws-shell-identity span,
.ws-shell-sidebar.is-collapsed .ws-shell-label,
.ws-shell-sidebar.is-collapsed .ws-shell-new > span,
.ws-shell-sidebar.is-collapsed .ws-shell-pages button > span { display:none; }
.ws-shell-sidebar.is-collapsed .ws-shell-identity { padding-inline:0; text-align:center; }
.ws-shell-sidebar.is-collapsed .ws-shell-new,
.ws-shell-sidebar.is-collapsed .ws-shell-pages button { justify-content:center; padding-inline:0; }
.ws-shell-sidebar.is-collapsed .ws-shell-recent { display:none; }
.ws-shell-identity { display:grid; gap:var(--space-1); padding:0 var(--space-2) var(--space-4); }
.ws-shell-identity strong { font-size: var(--font-control); font-weight:600; }
.ws-shell-identity span { font-size: var(--font-kicker); color:var(--text-muted); }
.ws-shell-new, .ws-shell-pages button { display:flex; align-items:center; gap:var(--space-2); min-height:var(--control-md); padding:var(--space-2) var(--space-2); border:0; border-radius:var(--radius-dialog); background:transparent; color:var(--text-primary); text-align:left; font:inherit; font-size: var(--font-control); cursor:pointer; transition:background var(--duration-fast) var(--ease-standard), transform var(--duration-fast) var(--ease-out); }
.ws-shell-new:active, .ws-shell-pages button:active { transform:translateY(1px); }
.ws-shell-new { flex-shrink:0; margin-bottom:var(--space-5); }
.ws-shell-pages { display:grid; gap:var(--space-1); flex-shrink:0; }
.ws-shell-label { display:block; padding:var(--space-2) var(--space-2) var(--space-2); font-size: var(--font-kicker); color:var(--text-muted); }
.ws-shell-pages button svg { color:var(--text-secondary); }
.ws-shell-pages button[aria-current] { background:var(--sidebar-selected); }
.ws-shell-new:hover, .ws-shell-pages button:hover { background:var(--sidebar-hover); }
.ws-shell-new:disabled { opacity:.45; cursor:not-allowed; }
.ws-shell-recent { display:flex; flex-direction:column; flex:1; min-height:0; padding-top:var(--space-6); }
.ws-shell-main { position:relative; display:flex; flex:1; flex-direction:column; min-width:0; min-height:0; }
.ws-shell-top { display:flex; align-items:center; flex-shrink:0; gap:var(--space-3); height:60px; padding:var(--space-2) var(--space-6); background:transparent; }
.ws-shell-toggle { display:grid; place-items:center; flex:0 0 30px; width:30px; height:var(--control-sm); border:0; border-radius:var(--radius-dialog); background:transparent; color:var(--text-secondary); cursor:pointer; transition:background var(--duration-fast) var(--ease-standard); }
.ws-shell-toggle:hover { background:var(--surface-subtle); }
.ws-shell-collapse { display:grid; place-items:center; flex:0 0 30px; width:30px; height:var(--control-sm); border:0; border-radius:var(--radius-dialog); background:transparent; color:var(--text-secondary); cursor:pointer; transition:background var(--duration-fast) var(--ease-standard); }
.ws-shell-collapse:hover { background:var(--surface-subtle); }
.ws-shell-stage { display:flex; align-items:center; flex-shrink:0; min-width:0; }
.ws-shell-heading { display:flex; align-items:center; flex-shrink:0; gap:var(--space-3); height:62px; padding:var(--space-2) var(--space-8) var(--space-4); background:transparent; }
/* 内容与输入框的共同容器。默认（工作区）下输入框相对 .ws-shell-main 绝对定位，
   容器只负责把内容撑满剩余高度；流式（首页）下容器整体滚动，输入框回到文档流末尾。 */
.ws-shell-body { display:flex; flex:1; flex-direction:column; min-height:0; min-width:0; }
.ws-shell-content { display:flex; flex:1; min-height:0; min-width:0; }
.ws-shell-composer { position:absolute; z-index:5; bottom:14px; left:50%; transform:translateX(-50%); width:min(760px, calc(100% - 48px)); background:transparent; }
/* 流式布局：滚动条长在容器上，输入框成为最后一行内容，滚到底才出现。 */
.ws-shell-body.is-flow { overflow-y:auto; overscroll-behavior:contain; scrollbar-gutter:stable; }
.ws-shell-body.is-flow .ws-shell-content { flex:none; }
/* margin-top:auto 让"内容不足一屏"时输入框贴住容器底部（观感与改版前一致），
   内容超出后 auto 归零，输入框自然落到内容末尾并随之滚动。 */
.ws-shell-body.is-flow .ws-shell-composer { position:static; left:auto; bottom:auto; transform:none; width:min(780px, calc(100% - 40px)); margin:auto auto var(--space-8); }
/* 内层不再自己滚，也不留底部占位，否则会出现"双层滚动 + 空白尾巴"。 */
.ws-shell-body.is-flow :deep(.message-scroller) { overflow:visible; }
.ws-shell-body.is-flow :deep(.chat-home) { height:auto; grid-template-rows:auto auto auto auto auto; }
.ws-shell-body.is-flow :deep(.chat-home.fast-chat) { grid-template-rows:auto auto; }
/* 空状态原本靠 min-height:100% 在整屏里居中；流式下父级高度变为 auto，
   改用一个有限的最小高度，既保持居中观感又不会把页面撑出滚动条。 */
.ws-shell-body.is-flow :deep(.message-list) { min-height:min(420px, 52vh); }
.ws-shell-body.is-flow .ws-shell-content :deep(.stage-scroll),
.ws-shell-body.is-flow .ws-shell-content :deep(.workspace-messages),
.ws-shell-body.is-flow .ws-shell-content :deep(.home-message-scroller) { padding-bottom:0; scroll-padding-bottom:0; mask-image:none; }
.ws-shell-content :deep(.stage-scroll), .ws-shell-content :deep(.workspace-messages) {
  padding-bottom:var(--ws-composer-space);
  scroll-padding-bottom:var(--ws-composer-space);
  mask-image:linear-gradient(to bottom, black calc(100% - var(--ws-composer-space)), transparent calc(100% - var(--ws-composer-space) + 16px));
}
.ws-shell-content :deep(.home-message-scroller) { padding-bottom:var(--ws-composer-space); scroll-padding-bottom:var(--ws-composer-space); }
:deep(.message-composer) { border-radius:var(--radius-panel); box-shadow:var(--shadow-card); }
:deep(.message-composer footer > span) { min-width:0; overflow-wrap:anywhere; }
:deep(.message-composer button), :deep(select) { border-radius:var(--radius-dialog); }
:deep(.composer-domain-menu), :deep(.composer-domain-chip) { border-radius:var(--radius-panel); }
button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
@media(max-width:1000px) { .ws-shell-sidebar:not(.is-collapsed) { flex-basis:204px; } .ws-shell-top { padding-inline:var(--space-4); gap:var(--space-2); } .ws-shell-stage { display:flex; align-items:center; flex-shrink:0; min-width:0; }
.ws-shell-heading { padding-inline:var(--space-6); } }
@media(max-width:600px) { .ws-shell-sidebar:not(.is-collapsed) { flex-basis:180px; padding-inline:var(--space-2); } .ws-shell-top { padding-inline:var(--space-3); gap:var(--space-2); } .ws-shell-stage { display:flex; align-items:center; flex-shrink:0; min-width:0; }
.ws-shell-heading { padding:var(--space-2) var(--space-4) var(--space-3); } .ws-shell-composer { bottom:10px; width:calc(100% - 24px); } }
@media(max-width:480px) {
  .ws-shell-sidebar:not(.is-collapsed) { flex-basis:160px; }
  .ws-shell-top { flex-wrap:wrap; height:92px; padding:var(--space-2) var(--space-2); }
  .ws-shell-top :deep(.toolbar-mode) { flex-basis:100%; min-width:0; }
  .ws-shell-top :deep(.ui-dropdown), .ws-shell-top :deep(.ui-dropdown-trigger) { min-width:0; width:100%; }
  .ws-shell-top :deep(select) { max-width:100%; }
  .ws-shell-heading :deep(small) { display:none; }
}
</style>
