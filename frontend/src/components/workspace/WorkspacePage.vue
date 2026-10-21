<script setup lang="ts">
import { ref, type Component } from 'vue'
import WorkspaceShell from './WorkspaceShell.vue'

/** 任务中心 / 资产 / 设置等无聊天页面复用同一 Workspace Shell，不再各自造 Header。 */
const props = withDefaults(defineProps<{ identity: string; subtitle?: string; activePage: string; navigation: { id: string; label: string; icon: Component }[] }>(), { subtitle: '' })
const navOpen = ref(!window.matchMedia('(max-width: 900px)').matches)
/* 桌面端图标条折叠态：与 navOpen（抽屉显隐）独立，跨页共用同一 key 保持状态一致。
   localStorage 在 SSR / 单元测试环境下不存在，必须容错，否则 setup 阶段直接抛错。 */
const navCollapsedKey = 'kantoku-ws-nav-collapsed'
function readNavCollapsed(): boolean {
  try { return localStorage.getItem(navCollapsedKey) === '1' } catch { return false }
}
const navCollapsed = ref(readNavCollapsed())
function toggleNavCollapsed(value: boolean): void {
  navCollapsed.value = value
  try { localStorage.setItem(navCollapsedKey, value ? '1' : '0') } catch { /* 忽略写入失败 */ }
}
</script>

<template>
  <WorkspaceShell
    :identity="props.identity"
    :subtitle="props.subtitle"
    :navigation="props.navigation"
    :active-page="props.activePage"
    v-model:navigation-open="navOpen"
    :navigation-collapsed="navCollapsed"
    :creating-conversation="false"
    :workspace-label="`${props.identity} 工作区`"
    @update:navigation-collapsed="toggleNavCollapsed"
  >
    <template #toolbar><slot name="toolbar" /></template>
    <template #stage><slot name="stage" /></template>
    <div class="ws-page"><slot /></div>
  </WorkspaceShell>
</template>

<style scoped>
.ws-page { flex: 1; min-width: 0; min-height: 0; overflow: auto; padding:24px 28px 34px; background: var(--canvas); }
.ws-page :deep(.page) { min-height: 0; padding:0; }
.ws-page :deep(.page-header) { margin-bottom:20px; }
</style>
