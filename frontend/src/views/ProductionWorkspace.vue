<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { PanelRight } from 'lucide-vue-next'
import WorkspaceInspectorDrawer from '../components/workspace/WorkspaceInspectorDrawer.vue'
import WorkspaceStageNav from '../components/workspace/WorkspaceStageNav.vue'
import type { StageItem } from '../components/workspace/stageTypes'
import DirectorWorkspace from '../components/DirectorWorkspace.vue'
import { resolveComicPreviewUrl } from '../domains/comic/workspacePresentation'
import { presenterFor } from '../domains/presenters'
import { navigate } from '../router'
import { getArtifactContentUrl, getArtifacts, getEvents, getRun, getTaskImageUrl, resumeRun, subscribeRunEvents, type RuntimeEvent } from '../services/core'
import type { CoreArtifact, CoreRun } from '../types'
import HomeView from './HomeView.vue'

const props = defineProps<{ domain: string; runId?: string }>()
const presenter = computed(() => presenterFor(props.domain))
const run = ref<CoreRun | null>(null)
const artifacts = ref<CoreArtifact[]>([])
const events = ref<RuntimeEvent[]>([])
const chatActive = ref(false)
const resuming = ref(false)
const resumeError = ref('')
const selectedArtifactId = ref('')
const previewUrl = ref('')
const previewLoading = ref(false)
const inspectorOpen = ref(false)
const activeStage = ref('')
/** 第二层侧栏点节点：切换当前阶段并打开详情，中央区域跟随该阶段。 */
function openStage(nodeId: string): void {
  if (nodeId === 'conversation') { activeStage.value = ''; inspectorOpen.value = false; return }
  activeStage.value = nodeId
  inspectorOpen.value = true
}
let unsubscribe: (() => void) | null = null

const isComic = computed(() => props.domain === 'comic')
const selectedArtifact = computed(() => artifacts.value.find((item) => item.id === selectedArtifactId.value) ?? null)
const visualArtifacts = computed(() => artifacts.value.filter((item) => item.type === 'image' || item.type === 'video'))
const runStatusLabels: Record<string, string> = { pending: '待执行', running: '制作中', waiting: '等待确认', completed: '已完成', failed: '失败', cancelled: '已取消' }
const statusText = computed(() => runStatusLabels[run.value?.status ?? ''] ?? run.value?.status ?? '待开始')
const previewKey = computed(() => {
  if (!isComic.value) return ''
  if (selectedArtifact.value) return `artifact:${selectedArtifact.value.id}`
  if (visualArtifacts.value.length) return ''
  const state = run.value?.state
  return state?.image_path && state.request_id ? `task:${String(state.request_id)}` : ''
})
const externalWait = computed(() => {
  if (run.value?.status !== 'waiting') return null
  const event = [...events.value].reverse().find((item) => item.event_type === 'run_waiting')
  const kind = String(event?.payload.kind ?? '')
  return ['external_job_pending', 'needs_reconciliation'].includes(kind) ? kind : null
})

/** Workflow 不再常驻一列：这里只把它渲染成顶部可点击的阶段导航。 */
const stages = computed<StageItem[]>(() => {
  if (!run.value || !run.value.nodes.length) {
    return presenter.value.workflow.map((node, index) => ({
      id: node.id,
      label: node.label,
      status: chatActive.value && index === 0 ? 'running' : 'pending',
    }))
  }
  return run.value.nodes.map((node) => ({
    id: node.node_id,
    label: presenter.value.nodeLabel(node.node_id),
    status: node.status as StageItem['status'],
  }))
})

const activeStageDetail = computed(() => {
  const stage = stages.value.find((item) => item.id === activeStage.value)
  if (!stage) return null
  const execution = run.value?.nodes.find((node) => node.node_id === stage.id)
  return {
    stage,
    artifacts: artifacts.value.filter((item) => item.node_id === stage.id),
    startedAt: execution?.started_at ?? null,
    completedAt: execution?.completed_at ?? null,
  }
})

async function refresh(): Promise<void> {
  if (!props.runId) return
  const [nextRun, nextArtifacts, nextEvents] = await Promise.all([
    getRun(props.runId), getArtifacts(props.runId), getEvents(props.runId),
  ])
  run.value = nextRun
  artifacts.value = nextArtifacts ?? []
  events.value = nextEvents
}

watch(artifacts, (items) => {
  if (selectedArtifactId.value && items.some((item) => item.id === selectedArtifactId.value)) return
  selectedArtifactId.value = [...items].reverse().find((item) => item.type === 'image' || item.type === 'video')?.id ?? ''
})

watch(previewKey, async (key, _previous, onCleanup) => {
  let cancelled = false
  onCleanup(() => { cancelled = true })
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
  previewUrl.value = ''
  previewLoading.value = !!key
  if (!key) return
  const state = run.value?.state
  const requestId = state?.image_path && state.request_id ? String(state.request_id) : null
  const url = await resolveComicPreviewUrl(selectedArtifact.value, requestId, getArtifactContentUrl, getTaskImageUrl)
  if (cancelled) {
    if (url) URL.revokeObjectURL(url)
    return
  }
  previewUrl.value = url ?? ''
  previewLoading.value = false
}, { immediate: true })

async function queryOriginalJob(): Promise<void> {
  if (!run.value || externalWait.value !== 'external_job_pending' || resuming.value) return
  resuming.value = true
  resumeError.value = ''
  try {
    await resumeRun(run.value.id)
    await refresh()
  } catch (error) {
    resumeError.value = error instanceof Error ? error.message : '查询原任务失败'
  } finally {
    resuming.value = false
  }
}

function follow(): void {
  unsubscribe?.()
  if (!props.runId) return
  unsubscribe = subscribeRunEvents(props.runId, events.value.at(-1)?.sequence ?? 0, {
    onEvent: (event) => {
      events.value = [...events.value.filter((item) => item.sequence !== event.sequence), event]
      void refresh()
    },
    onEnd: () => { void refresh() },
  })
}

watch(() => props.runId, async () => {
  unsubscribe?.()
  selectedArtifactId.value = ''
  if (props.runId) {
    await refresh()
    follow()
    return
  }
  run.value = null
  artifacts.value = []
  events.value = []
}, { immediate: true })
onBeforeUnmount(() => {
  unsubscribe?.()
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
})
</script>

<template>
  <section v-if="isComic" class="domain-workspace comic-workspace">
    <DirectorWorkspace :key="runId ?? 'current-project'" :initial-run-id="runId" @new-project="navigate({ name: 'workspace', domain: 'comic' })">
      <template #works>
        <section v-if="run && run.workflow !== 'comic.director'" class="comic-preview" aria-label="已有制作结果">
          <header class="comic-preview-header"><strong>已有制作记录</strong><small>{{ statusText }}</small></header>
          <p v-if="externalWait" class="pane-note">{{ externalWait === 'needs_reconciliation' ? '原任务需要人工查账，不会重新付费提交。' : '供应商尚未完成；继续查询沿用原任务。' }}</p>
          <button v-if="externalWait === 'external_job_pending'" class="ui-button sm" :disabled="resuming" @click="queryOriginalJob">继续查询原任务</button>
          <p v-if="resumeError" role="alert">{{ resumeError }}</p>
          <div class="comic-preview-stage">
            <video v-if="previewUrl && selectedArtifact?.type === 'video'" :src="previewUrl" controls preload="metadata" />
            <img v-else-if="previewUrl" :src="previewUrl" alt="已保存的真实制作产物" />
            <p v-else class="pane-note">{{ previewLoading ? '正在载入真实产物' : '此任务暂无可展示的真实产物。' }}</p>
          </div>
          <button v-for="item in visualArtifacts" :key="item.id" class="ui-button quiet sm" @click="selectedArtifactId = item.id">{{ presenter.artifactName(item.source, item.type) }} · v{{ item.version }}</button>
          <button class="text-action" @click="navigate({ name: 'task_run', runId: run.id })">查看原任务、审批与追踪记录</button>
        </section>
        <p v-else class="pane-note">当前会话暂无图片生产记录。确认导演方案后可选择“生成当前画面”，在原任务界面查看费用、生成、检查和图片结果。</p>
      </template>
    </DirectorWorkspace>
  </section>

  <!-- 非 Comic 领域：统一 Shell —— 左侧导航 + 中央工作区 + 顶部阶段导航 + 按需详情抽屉 -->
  <section v-else class="domain-workspace ws-domain">
    <HomeView
      :key="domain"
      :domain="domain"
      :initial-run-id="runId"
      :runs="run ? [run] : []"
      :approvals="[]"
      embedded
      @refresh="refresh"
      @run-created="(created) => navigate({ name: 'workspace_run', domain, runId: created.id })"
      @chatting="(active) => (chatActive = active)"
      @select-stage="openStage"
    >
      <template #workspace-stage>
        <WorkspaceStageNav :stages="stages" :active-id="activeStage" />
      </template>
      <template #workspace-toolbar>
        <button class="ui-button quiet sm" :aria-expanded="inspectorOpen" @click="inspectorOpen = !inspectorOpen"><PanelRight :size="15" /> 详情</button>
      </template>
    </HomeView>

    <WorkspaceInspectorDrawer :open="inspectorOpen" title="任务详情" @close="inspectorOpen = false">
      <section v-if="activeStageDetail" class="drawer-section">
        <span class="section-kicker">当前阶段</span>
        <strong class="drawer-title">{{ activeStageDetail.stage.label }}</strong>
        <dl class="inspector-meta">
          <div><dt>状态</dt><dd>{{ runStatusLabels[activeStageDetail.stage.status] ?? activeStageDetail.stage.status }}</dd></div>
          <div v-if="activeStageDetail.startedAt"><dt>开始</dt><dd>{{ activeStageDetail.startedAt }}</dd></div>
          <div v-if="activeStageDetail.completedAt"><dt>结束</dt><dd>{{ activeStageDetail.completedAt }}</dd></div>
        </dl>
        <ul v-if="activeStageDetail.artifacts.length" class="inspector-artifacts">
          <li v-for="item in activeStageDetail.artifacts" :key="item.id"><strong>{{ presenter.artifactName(item.source, item.type) }}</strong><small>{{ item.type }}</small></li>
        </ul>
        <p v-else class="pane-note">此阶段尚未产出 Artifact。</p>
      </section>
      <section v-if="run" class="drawer-section">
        <span class="section-kicker">任务</span>
        <dl class="inspector-meta">
          <div><dt>状态</dt><dd>{{ statusText }}</dd></div>
          <div><dt>当前节点</dt><dd>{{ run.current_node ? presenter.nodeLabel(run.current_node) : '—' }}</dd></div>
          <div><dt>费用</dt><dd>¥{{ (run.cost_fen / 100).toFixed(2) }}</dd></div>
          <div><dt>Run</dt><dd>{{ run.id.slice(0, 12) }}</dd></div>
        </dl>
        <button class="text-action" @click="navigate({ name: 'task_run', runId: run.id })">在任务中心查看 →</button>
      </section>
      <section v-if="artifacts.length" class="drawer-section">
        <span class="section-kicker">全部产物 · {{ artifacts.length }}</span>
        <ul class="inspector-artifacts"><li v-for="item in artifacts" :key="item.id"><strong>{{ presenter.artifactName(item.source, item.type) }}</strong><small>{{ item.type }}</small></li></ul>
      </section>
      <section v-if="externalWait" class="drawer-section" role="status">
        <span class="section-kicker">供应商状态</span>
        <p class="pane-note">{{ externalWait === 'needs_reconciliation' ? '原任务缺少供应商任务 ID，需要人工查账；系统不会重新付费提交。' : '供应商任务尚未完成。继续查询会沿用原任务 ID。' }}</p>
        <button v-if="externalWait === 'external_job_pending'" class="ui-button sm" :disabled="resuming" @click="queryOriginalJob">{{ resuming ? '查询中…' : '继续查询原任务' }}</button>
        <p v-if="resumeError" class="pane-note" role="alert">{{ resumeError }}</p>
      </section>
      <p v-if="!artifacts.length && !run" class="pane-note">任务产物和技术信息会在这里出现。</p>
    </WorkspaceInspectorDrawer>
  </section>
</template>

<style scoped>
.ws-domain { position: relative; display: flex; min-height: 0; background: var(--surface); }
.drawer-section { display: grid; gap:8px; margin-bottom:20px; }
.drawer-title { font-size: 14px; font-weight: 600; }
</style>
