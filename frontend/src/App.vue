<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import CommandPalette from './components/layout/CommandPalette.vue'
import SidebarNav from './components/layout/SidebarNav.vue'
import TopBar from './components/layout/TopBar.vue'
import type { DomainDefinition } from './domains'
import HomeView from './views/HomeView.vue'
import ProductionWorkspace from './views/ProductionWorkspace.vue'
import TasksCenter from './views/TasksCenter.vue'
import AssetsLibrary from './views/AssetsLibrary.vue'
import { navigate, parse, route } from './router'
import { disposeAll, getApprovals, getRuns } from './services/core'
import type { CoreApproval, CoreRun } from './types'

/* 原有排版骨架：侧栏导航 + 顶栏 + 页面舞台；工作逻辑全部来自真实 Core 数据。 */
const runs = ref<CoreRun[]>([])
const approvals = ref<CoreApproval[]>([])
const sidebarCollapsed = ref(false)
const commandOpen = ref(false)
let timer: number | undefined

const activeDomain = computed(() => route.value.domain ?? 'studio')

const titles: Record<string, string> = {
  home: '首页',
  workspace: '工作区',
  workspace_run: '任务会话',
  tasks: '任务中心',
  task_run: '运行详情',
  task_batch: '批次详情',
  assets: '资产',
  settings: '设置',
}
const title = computed(() => `${titles[route.value.name] ?? 'Kantoku'}`)

async function refreshCore(): Promise<void> {
  const [runData, approvalData] = await Promise.all([getRuns(), getApprovals()])
  if (runData) runs.value = runData
  if (approvalData) approvals.value = approvalData
}

function go(path: string): void {
  navigate(parse(path))
}

function openDomain(domain: DomainDefinition): void {
  navigate({ name: 'workspace', domain: domain.id })
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
  window.addEventListener('keydown', shortcut)
  void refreshCore()
  timer = window.setInterval(() => void refreshCore(), 4000)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', shortcut)
  if (timer !== undefined) window.clearInterval(timer)
  disposeAll()
})
</script>

<template>
  <div class="kantoku-shell" :class="{ 'sidebar-collapsed': sidebarCollapsed }">
    <SidebarNav
      :route-name="route.name"
      :active-domain-id="activeDomain"
      :task-count="runs.filter((item) => ['pending', 'running', 'waiting'].includes(item.status)).length"
      :collapsed="sidebarCollapsed"
      @navigate="go"
      @domain="openDomain"
      @toggle="sidebarCollapsed = !sidebarCollapsed"
    />
    <section class="app-stage">
      <TopBar :title="title" :busy="false" @command="commandOpen = true" />
      <HomeView
        v-if="route.name === 'home'"
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
      <AssetsLibrary v-else-if="route.name === 'assets'" />
      <main v-else class="page">
        <header class="page-header">
          <div><span class="section-kicker">模型配置</span><h1>设置</h1><p>模型与供应商来自本机 settings；密钥只保存在 .env，不在页面显示。</p></div>
        </header>
      </main>
    </section>
    <CommandPalette
      :open="commandOpen"
      @close="commandOpen = false"
      @navigate="go"
      @domain="(id: string) => navigate({ name: 'workspace', domain: id })"
    />
  </div>
</template>
