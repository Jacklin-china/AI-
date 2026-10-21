<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Boxes, Settings } from 'lucide-vue-next'
import CommandPalette from './components/layout/CommandPalette.vue'
import SidebarNav from './components/layout/SidebarNav.vue'
import WorkspacePage from './components/workspace/WorkspacePage.vue'
import HomeView from './views/HomeView.vue'
import ProductionWorkspace from './views/ProductionWorkspace.vue'
import TasksCenter from './views/TasksCenter.vue'
import AssetsLibrary from './views/AssetsLibrary.vue'
import { navigate, parse, route, workspaceEntry } from './router'
import { disposeAll, getApprovals, getRuns } from './services/core'
import type { CoreApproval, CoreRun } from './types'

/* 原有排版骨架：侧栏导航 + 顶栏 + 页面舞台；工作逻辑全部来自真实 Core 数据。 */
const runs = ref<CoreRun[]>([])
const approvals = ref<CoreApproval[]>([])
/* 用户主动折叠偏好：持久化到 localStorage（与既有"会话 ID 存 localStorage"的做法一致，不引入状态库）。
   首次访问时按窗口宽度给默认值：窄窗默认收起，宽屏默认展开。 */
const STORAGE_KEY = 'kantoku-sidebar-collapsed'
function readCollapsed(): boolean {
  // SSR / 单元测试环境没有 localStorage 与 window，必须整体容错。
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved !== null) return saved === '1'
  } catch { /* 隐私模式或非浏览器环境：忽略，退回宽度判断 */ }
  try {
    return window.matchMedia('(max-width: 800px)').matches
  } catch { return false }
}
const sidebarCollapsed = ref(readCollapsed())
function toggleSidebar(): void {
  sidebarCollapsed.value = !sidebarCollapsed.value
  try { localStorage.setItem(STORAGE_KEY, sidebarCollapsed.value ? '1' : '0') } catch { /* 忽略写入失败 */ }
}
const commandOpen = ref(false)
let timer: number | undefined
let coreEpoch = 0, coreMounted = false, disposed = false
let coreRequest: { epoch: number; promise: Promise<void> } | null = null

const activeDomain = computed(() => route.value.domain ?? 'comic')
const comicWorkspace = computed(() => ['workspace', 'workspace_run'].includes(route.value.name) && route.value.domain === 'comic')
/* 工作区页默认用图标 rail（给内容让出宽度）；其余页面用完整侧栏。
   这里只决定"外观形态"，不再锁死用户的折叠操作。 */
const conversationWorkspace = computed(() => comicWorkspace.value)
/* 合成值：工作区 rail 或用户主动折叠时，外壳列才收窄。 */
const shellCollapsed = computed(() => conversationWorkspace.value || sidebarCollapsed.value)
const settingsNavigation = [
  { id: 'providers', label: '模型与供应商', icon: Boxes },
  { id: 'settings', label: '设置', icon: Settings },
]

async function refreshCore(): Promise<void> {
  if (disposed || comicWorkspace.value) return
  const epoch = coreEpoch
  if (coreRequest?.epoch === epoch) return coreRequest.promise
  const pending = Promise.resolve().then(async () => {
    try {
      if (disposed || comicWorkspace.value || epoch !== coreEpoch) return
      const [runData, approvalData] = await Promise.all([getRuns(), getApprovals()])
      if (disposed || comicWorkspace.value || epoch !== coreEpoch) return
      if (runData) runs.value = runData
      if (approvalData) approvals.value = approvalData
    } finally { if (coreRequest?.promise === pending) coreRequest = null }
  })
  coreRequest = { epoch, promise: pending }
  return pending
}

function syncCorePolling(): void {
  if (timer !== undefined) window.clearInterval(timer)
  timer = undefined
  // Comic 工作区自行读取所属任务；全局列表不参与其首屏和执行状态。
  if (disposed || comicWorkspace.value) return
  void refreshCore()
  timer = window.setInterval(() => void refreshCore(), 4000)
}
watch(comicWorkspace, () => {
  coreEpoch++
  if (coreMounted) syncCorePolling()
}, { flush: 'sync' })

function go(path: string): void {
  const next = parse(path)
  navigate(next.name === 'workspace' && !next.workId && !next.conversationId
    && !next.workspacePage ? workspaceEntry(next.domain) : next)
}

function shortcut(event: KeyboardEvent): void {
  const target = event.target
  if (target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))) return
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    commandOpen.value = !commandOpen.value
  }
}

onMounted(() => {
  coreMounted = true
  window.addEventListener('keydown', shortcut)
  syncCorePolling()
})
onBeforeUnmount(() => {
  disposed = true; coreEpoch++
  window.removeEventListener('keydown', shortcut)
  if (timer !== undefined) window.clearInterval(timer)
  disposeAll()
})
</script>

<template>
  <div class="kantoku-shell" :class="{ 'sidebar-collapsed': shellCollapsed, 'comic-app-shell': conversationWorkspace }">
    <SidebarNav
      :route-name="route.name"
      :active-domain-id="activeDomain"
      :task-count="runs.filter((item) => ['pending', 'running', 'waiting'].includes(item.status)).length"
      :collapsed="shellCollapsed"
      :rail="conversationWorkspace"
      @navigate="go"
      @toggle="toggleSidebar"
    />
    <section class="app-stage">
      <Transition name="route" mode="out-in">
        <HomeView
          v-if="route.name === 'home'"
          key="home"
          :runs="runs"
          :approvals="approvals"
          :busy="false"
          @refresh="refreshCore"
        />
        <ProductionWorkspace
          v-else-if="route.name === 'workspace' || route.name === 'workspace_run'"
          :key="`${route.domain}:${route.runId ?? 'new'}`"
          :domain="route.domain ?? 'studio'"
          :run-id="route.runId"
        />
        <TasksCenter
          v-else-if="route.name === 'tasks'"
          :key="`tasks:${route.tab ?? 'running'}`"
          :tab="route.tab"
        />
        <TasksCenter
          v-else-if="route.name === 'task_run'"
          :key="`task-run:${route.runId}`"
          tab="running"
          :focus-run-id="route.runId"
        />
        <TasksCenter
          v-else-if="route.name === 'task_batch'"
          :key="`task-batch:${route.batchId}`"
          tab="batches"
          :focus-batch-id="route.batchId"
        />
        <AssetsLibrary v-else-if="route.name === 'assets'" key="assets" />
        <WorkspacePage v-else key="settings" identity="设置" subtitle="模型 · 供应商 · 偏好" active-page="settings" :navigation="settingsNavigation">
          <template #toolbar><button class="ui-button quiet sm" @click="commandOpen = true">打开命令面板</button></template>
          <header class="page-header">
            <div><h1>模型与供应商</h1><p>模型与供应商来自本机 settings；密钥只保存在 .env，不在页面显示。</p></div>
          </header>
        </WorkspacePage>
      </Transition>
    </section>
    <CommandPalette
      :open="commandOpen"
      @close="commandOpen = false"
      @navigate="go"
      @domain="(id: string) => navigate(workspaceEntry(id))"
    />
  </div>
</template>
