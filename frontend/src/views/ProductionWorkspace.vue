<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Check, ChevronDown, Film, Image as ImageIcon, Images, MessageSquareText, PanelRight, X } from 'lucide-vue-next'
import { Pane, Splitpanes } from 'splitpanes'
import 'splitpanes/dist/splitpanes.css'
import WorkflowProgress, { type WorkflowStep } from '../components/WorkflowProgress.vue'
import { comicPhases, resolveComicPreviewUrl } from '../domains/comic/workspacePresentation'
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
const assetsOpen = ref(false)
const inspectorOpen = ref(false)
const previewOpen = ref(true)
const compactView = ref<'chat' | 'preview'>('chat')
let unsubscribe: (() => void) | null = null

const isComic = computed(() => props.domain === 'comic')
const selectedArtifact = computed(() => artifacts.value.find((item) => item.id === selectedArtifactId.value) ?? null)
const visualArtifacts = computed(() => artifacts.value.filter((item) => item.type === 'image' || item.type === 'video'))
const shotNumber = computed(() => Number(run.value?.state.shot_no) || null)
const shotPrompt = computed(() => String(run.value?.state.prompt ?? '').trim())
const runStatusLabels: Record<string, string> = { pending: '待执行', running: '制作中', waiting: '等待确认', completed: '已完成', failed: '失败', cancelled: '已取消' }
const statusText = computed(() => runStatusLabels[run.value?.status ?? ''] ?? run.value?.status ?? '待开始')
const phases = computed(() => comicPhases(run.value))
const currentStage = computed(() => run.value?.current_node && run.value.current_node !== '__end__'
  ? presenter.value.nodeLabel(run.value.current_node)
  : statusText.value)
const qcReason = computed(() => {
  const result = run.value?.state.qc_result
  return result && typeof result === 'object' && !Array.isArray(result)
    ? String((result as Record<string, unknown>).reason ?? '暂无检查说明')
    : ''
})
const inspectorHeading = computed(() => {
  const node = run.value?.current_node
  if (node === 'generate') return '图片生成'
  if (node === 'video') return '视频生成'
  if (node === 'qc' || node === 'human_review') return '视觉检查'
  return selectedArtifact.value ? '产物信息' : '制作详情'
})
const previewKey = computed(() => {
  if (!isComic.value) return ''
  if (selectedArtifact.value) return `artifact:${selectedArtifact.value.id}`
  if (visualArtifacts.value.length) return ''
  const state = run.value?.state
  return state?.image_path && state.request_id ? `task:${String(state.request_id)}` : ''
})
const nextAction = computed(() => {
  if (!run.value) return '描述故事、角色与想呈现的画面，开始创作。'
  if (run.value.status === 'waiting') return externalWait.value
    ? '原供应商任务仍需查询；打开制作详情继续查询原任务。'
    : '当前步骤需要你决定；请在对话中审核画面或费用。'
  if (run.value.status === 'running' || run.value.status === 'pending') return '正在执行当前步骤；产物完成后会自动出现在画布。'
  if (run.value.status === 'completed') return '本次制作已完成。可以继续提出修改或新镜头需求。'
  return '本次制作未完成。打开制作详情查看状态，或在对话中继续处理。'
})

const externalWait = computed(() => {
  if (run.value?.status !== 'waiting') return null
  const event = [...events.value].reverse().find((item) => item.event_type === 'run_waiting')
  const kind = String(event?.payload.kind ?? '')
  return ['external_job_pending', 'needs_reconciliation'].includes(kind) ? kind : null
})

const steps = computed<WorkflowStep[]>(() => {
  if (!run.value || !run.value.nodes.length) {
    return presenter.value.workflow.map((node, index) => ({
      id: node.id,
      name: node.label,
      status: chatActive.value && index === 0 ? 'running' : 'pending',
    }))
  }
  return run.value.nodes.map((node) => ({
    id: node.node_id,
    name: presenter.value.nodeLabel(node.node_id),
    status: node.status as WorkflowStep['status'],
  }))
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

function setCompactView(view: 'chat' | 'preview'): void {
  compactView.value = view
  if (view === 'preview') previewOpen.value = true
}

function togglePreview(): void {
  previewOpen.value = !previewOpen.value
  if (!previewOpen.value) compactView.value = 'chat'
}

watch(() => props.runId, async () => {
  unsubscribe?.()
  selectedArtifactId.value = ''
  assetsOpen.value = false
  inspectorOpen.value = false
  previewOpen.value = !!props.runId
  compactView.value = 'chat'
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
  <section v-if="isComic" class="domain-workspace comic-studio">
    <header class="comic-studio-header">
      <div class="comic-studio-title">
        <strong>漫剧创作</strong>
        <small v-if="run">{{ String(run.state.project ?? '当前制作') }}</small>
      </div>
      <nav v-if="phases.length" class="comic-phase-strip" aria-label="制作进度">
        <span v-for="phase in phases" :key="phase.id" :data-status="phase.status">
          <Check v-if="phase.status === 'completed'" :size="12" />
          <i v-else aria-hidden="true" />
          {{ phase.label }}
        </span>
      </nav>
      <div class="comic-studio-actions">
        <div v-if="run" class="comic-compact-switch" aria-label="工作区视图">
          <button type="button" :aria-pressed="compactView === 'chat'" @click="setCompactView('chat')"><MessageSquareText :size="14" /> 对话</button>
          <button type="button" :aria-pressed="compactView === 'preview'" @click="setCompactView('preview')"><Film :size="14" /> 预览</button>
        </div>
        <button v-if="run" type="button" class="comic-header-button" :aria-expanded="previewOpen" aria-label="结果画布" @click="togglePreview"><Film :size="16" /><span>{{ previewOpen ? '收起画布' : '打开画布' }}</span></button>
        <button v-if="run" type="button" class="comic-header-button" :aria-expanded="inspectorOpen" aria-label="制作详情" @click="inspectorOpen = !inspectorOpen; assetsOpen = false"><PanelRight :size="16" /><span>详情</span></button>
      </div>
    </header>
    <div class="comic-studio-body" :data-compact-view="compactView">
      <nav class="comic-tool-rail" aria-label="创作工具">
        <button type="button" :aria-expanded="assetsOpen" aria-label="制作资产" title="制作资产" @click="assetsOpen = !assetsOpen; inspectorOpen = false"><Images :size="19" /></button>
      </nav>
      <main class="comic-main">
        <Splitpanes class="comic-main-splitpanes" :class="{ 'has-run': !!run }">
          <Pane :size="run && previewOpen ? 72 : 100" :min-size="run && previewOpen ? 58 : 100">
            <div class="comic-director-chat">
              <HomeView :key="domain" :domain="domain" :embedded="true" :studio-focus="true" :initial-run-id="runId" :runs="run ? [run] : []" :approvals="[]" @refresh="refresh" @run-created="(created) => navigate({ name: 'workspace_run', domain, runId: created.id })" @chatting="(active) => (chatActive = active)" />
            </div>
          </Pane>
          <Pane v-if="run && previewOpen" :size="28" :min-size="22">
            <section class="comic-preview" aria-label="制作结果预览">
              <header class="comic-preview-header"><strong>结果预览</strong><small>{{ currentStage }}</small></header>
              <div class="comic-preview-stage">
                <video v-if="previewUrl && selectedArtifact?.type === 'video'" :src="previewUrl" controls preload="metadata" aria-label="当前视频产物" />
                <img v-else-if="previewUrl" :src="previewUrl" alt="当前制作的真实图片产物" />
                <div v-else class="comic-preview-empty" role="status"><strong>{{ previewLoading ? '正在载入产物' : run.state.image_path ? '图片暂时无法载入' : run.current_node === 'generate' ? '正在生成图片' : '结果会在完成后显示' }}</strong><p>{{ nextAction }}</p></div>
              </div>
              <footer v-if="selectedArtifact" class="comic-preview-footer">{{ presenter.artifactName(selectedArtifact.source, selectedArtifact.type) }} · 版本 {{ selectedArtifact.version }}</footer>
            </section>
          </Pane>
        </Splitpanes>
      </main>
      <aside v-if="assetsOpen" class="comic-flyout comic-assets-flyout" aria-label="制作资产" @keydown.esc="assetsOpen = false">
        <header><div><small>ASSETS</small><strong>制作资产</strong></div><button type="button" aria-label="关闭制作资产" @click="assetsOpen = false"><X :size="17" /></button></header>
        <div class="comic-flyout-content">
          <p v-if="run" class="comic-project-name">{{ String(run.state.project ?? '当前制作') }}</p>
          <p v-if="run" class="pane-note">镜头 {{ shotNumber ?? '—' }} · {{ statusText }}</p>
          <section class="comic-pane-group"><span class="section-kicker">已保存产物 · {{ visualArtifacts.length }}</span>
            <div v-if="visualArtifacts.length" class="comic-asset-list">
              <button v-for="item in visualArtifacts" :key="item.id" type="button" :class="{ active: selectedArtifactId === item.id }" @click="selectedArtifactId = item.id; assetsOpen = false">
                <ImageIcon v-if="item.type === 'image'" :size="15" /><Film v-else :size="15" />
                <span><strong>{{ presenter.artifactName(item.source, item.type) }}</strong><small>版本 {{ item.version }} · {{ item.type === 'video' ? '视频' : '图片' }}</small></span>
              </button>
            </div>
            <p v-else class="pane-note">暂无已保存的图片或视频。产物生成后会出现在这里。</p>
          </section>
          <p class="comic-assets-note">角色、场景与风格锁定资产尚未接入当前流程；这里仅展示真实保存的产物。</p>
        </div>
      </aside>
      <aside v-if="run && inspectorOpen" class="comic-flyout comic-inspector-flyout" aria-label="制作详情" @keydown.esc="inspectorOpen = false">
        <header><div><small>INSPECTOR</small><strong>{{ inspectorHeading }}</strong></div><button type="button" aria-label="关闭制作详情" @click="inspectorOpen = false"><X :size="17" /></button></header>
        <div class="comic-flyout-content">
          <section class="comic-current-context"><span class="section-kicker">当前阶段</span><strong>{{ currentStage }}</strong><p>{{ nextAction }}</p></section>
          <section v-if="externalWait" class="comic-pane-group" role="status"><p class="pane-note">{{ externalWait === 'needs_reconciliation' ? '原任务缺少供应商任务 ID，需要人工查账；系统不会重新付费提交。' : '供应商任务尚未完成；继续查询沿用原任务 ID。' }}</p><button v-if="externalWait === 'external_job_pending'" class="ui-button sm" :disabled="resuming" @click="queryOriginalJob">{{ resuming ? '查询中…' : '继续查询原任务' }}</button><p v-if="resumeError" class="pane-note" role="alert">{{ resumeError }}</p></section>
          <section v-if="run.state.qc_result" class="comic-pane-group"><span class="section-kicker">视觉检查</span><strong class="comic-qc-result">{{ run.state.qc_passed ? '预筛通过' : '需要复核' }}</strong><p class="pane-note">{{ qcReason }}</p></section>
          <section v-if="selectedArtifact" class="comic-pane-group"><span class="section-kicker">当前产物</span><p class="comic-selected-name">{{ presenter.artifactName(selectedArtifact.source, selectedArtifact.type) }} · 版本 {{ selectedArtifact.version }}</p></section>
          <details class="comic-advanced"><summary>高级信息 <ChevronDown :size="15" /></summary><div><dl class="inspector-meta"><div><dt>镜头</dt><dd>{{ shotNumber ?? '—' }}</dd></div><div><dt>视觉风格</dt><dd>{{ String(run.state.visual_style ?? '未设定') }}</dd></div><div><dt>已发生费用</dt><dd>¥{{ (run.cost_fen / 100).toFixed(2) }}</dd></div><div v-if="run.state.model"><dt>模型</dt><dd>{{ String(run.state.model) }}</dd></div><div v-if="run.state.seed"><dt>Seed</dt><dd>{{ String(run.state.seed) }}</dd></div></dl><section v-if="shotPrompt" class="comic-advanced-section"><span class="section-kicker">当前 Prompt</span><p class="comic-inspector-prompt">{{ shotPrompt }}</p></section><button class="text-action" @click="navigate({ name: 'task_run', runId: run.id })">查看完整任务记录 →</button></div></details>
        </div>
      </aside>
    </div>
  </section>
  <section v-else class="domain-workspace">
    <Splitpanes class="domain-splitpanes">
      <Pane :size="19" :min-size="14" :max-size="30">
        <aside class="workspace-pane workflow-pane">
          <header class="pane-heading"><span>Workflow</span><strong>{{ run ? '执行进度' : '完整流程' }}</strong></header>
          <WorkflowProgress v-if="steps.length" :steps="steps" compact />
          <p v-if="!run" class="pane-note">以上是{{ presenter.label }}的完整工作流。在对话中描述需求并确认后，这里会显示真实节点进度。</p>
          <section v-if="run" class="pane-section">
            <dl class="inspector-meta">
              <div><dt>状态</dt><dd>{{ run.status }}</dd></div>
              <div><dt>当前节点</dt><dd>{{ run.current_node ? presenter.nodeLabel(run.current_node) : '—' }}</dd></div>
              <div><dt>费用</dt><dd>¥{{ (run.cost_fen / 100).toFixed(2) }}</dd></div>
            </dl>
          </section>
          <section v-if="externalWait" class="pane-section" role="status">
            <p class="pane-note">{{ externalWait === 'needs_reconciliation' ? '原任务缺少供应商任务 ID，需要人工查账；系统不会重新付费提交。' : '供应商任务尚未完成。继续查询会沿用原任务 ID。' }}</p>
            <button v-if="externalWait === 'external_job_pending'" class="ui-button sm" :disabled="resuming" @click="queryOriginalJob">{{ resuming ? '查询中…' : '继续查询原任务' }}</button>
            <p v-if="resumeError" class="pane-note" role="alert">{{ resumeError }}</p>
          </section>
        </aside>
      </Pane>
      <Pane :size="57" :min-size="34">
        <div class="workspace-pane canvas-pane">
          <HomeView
            :key="domain"
            :domain="domain"
            :embedded="true"
            :initial-run-id="runId"
            :runs="run ? [run] : []"
            :approvals="[]"
            @refresh="refresh"
            @run-created="(created) => navigate({ name: 'workspace_run', domain, runId: created.id })"
            @chatting="(active) => (chatActive = active)"
          />
        </div>
      </Pane>
      <Pane :size="24" :min-size="16" :max-size="38">
        <aside class="workspace-pane workspace-inspector">
          <header class="pane-heading"><span>Inspector</span><strong>任务详情</strong></header>
          <section v-if="artifacts.length" class="pane-section">
            <span class="section-kicker">Artifacts</span>
            <ul class="inspector-artifacts"><li v-for="item in artifacts" :key="item.id"><strong>{{ presenter.artifactName(item.source, item.type) }}</strong><small>{{ item.type }}</small></li></ul>
          </section>
          <section v-if="run" class="pane-section">
            <dl class="inspector-meta"><div><dt>Run ID</dt><dd>{{ run.id.slice(0, 12) }}</dd></div><div><dt>Workflow</dt><dd>{{ run.workflow }}</dd></div></dl>
            <button class="text-action" @click="navigate({ name: 'task_run', runId: run.id })">在任务中心查看 →</button>
          </section>
          <p v-if="!artifacts.length && !run" class="inspector-empty">任务产物和技术信息会在这里出现。</p>
        </aside>
      </Pane>
    </Splitpanes>
  </section>
</template>
