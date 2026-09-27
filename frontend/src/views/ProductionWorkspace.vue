<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Film, Image as ImageIcon, Layers3 } from 'lucide-vue-next'
import { Pane, Splitpanes } from 'splitpanes'
import 'splitpanes/dist/splitpanes.css'
import WorkflowProgress, { type WorkflowStep } from '../components/WorkflowProgress.vue'
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
let unsubscribe: (() => void) | null = null

const isComic = computed(() => props.domain === 'comic')
const selectedArtifact = computed(() => artifacts.value.find((item) => item.id === selectedArtifactId.value) ?? null)
const visualArtifacts = computed(() => artifacts.value.filter((item) => item.type === 'image' || item.type === 'video'))
const shotNumber = computed(() => Number(run.value?.state.shot_no) || null)
const shotPrompt = computed(() => String(run.value?.state.prompt ?? '').trim())
const runStatusLabels: Record<string, string> = { pending: '待执行', running: '制作中', waiting: '等待确认', completed: '已完成', failed: '失败', cancelled: '已取消' }
const statusText = computed(() => runStatusLabels[run.value?.status ?? ''] ?? run.value?.status ?? '待开始')
const currentStage = computed(() => run.value?.current_node
  ? presenter.value.nodeLabel(run.value.current_node)
  : statusText.value)
const previewKey = computed(() => {
  if (!isComic.value) return ''
  if (selectedArtifact.value) return `artifact:${selectedArtifact.value.id}`
  if (visualArtifacts.value.length) return ''
  const state = run.value?.state
  return state?.image_path && state.request_id ? `task:${String(state.request_id)}` : ''
})
const nextAction = computed(() => {
  if (!run.value) return '在下方描述故事、角色与想呈现的画面，开始创作。'
  if (run.value.status === 'waiting') return externalWait.value
    ? '原供应商任务仍需查询；请使用左侧的「继续查询原任务」。'
    : '当前步骤需要你决定；请在下方审核画面或费用。'
  if (run.value.status === 'running' || run.value.status === 'pending') return '正在执行当前步骤；产物完成后会自动出现在画布。'
  if (run.value.status === 'completed') return '本次制作已完成。可以在下方继续提出修改或新镜头需求。'
  return '本次制作未完成。请查看下方对话与右侧状态，确认失败原因。'
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
  const url = key.startsWith('artifact:')
    ? await getArtifactContentUrl(key.slice(9)).catch(() => null)
    : await getTaskImageUrl(key.slice(5)).catch(() => null)
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
  <section class="domain-workspace">
    <Splitpanes class="domain-splitpanes">
      <Pane :size="19" :min-size="14" :max-size="30">
        <aside v-if="isComic" class="workspace-pane workflow-pane comic-assets-pane">
          <header class="pane-heading"><span>Assets</span><strong>制作资产</strong></header>
          <section class="comic-pane-group">
            <span class="section-kicker">当前制作</span>
            <p class="comic-project-name">{{ run ? String(run.state.project ?? '未命名项目') : '尚未开始制作' }}</p>
            <p class="pane-note">{{ run ? `镜头 ${shotNumber ?? '—'} · ${statusText}` : '向下方 AI 导演描述创意即可开始。' }}</p>
          </section>
          <section class="comic-pane-group">
            <span class="section-kicker">真实产物 · {{ visualArtifacts.length }}</span>
            <div v-if="visualArtifacts.length" class="comic-asset-list">
              <button v-for="item in visualArtifacts" :key="item.id" type="button" :class="{ active: selectedArtifactId === item.id }" @click="selectedArtifactId = item.id">
                <ImageIcon v-if="item.type === 'image'" :size="15" /><Film v-else :size="15" />
                <span><strong>{{ presenter.artifactName(item.source, item.type) }}</strong><small>版本 {{ item.version }} · {{ item.type === 'video' ? '视频' : '图片' }}</small></span>
              </button>
            </div>
            <p v-else class="pane-note">暂无已保存的图片或视频；生成结果会自动加入这里。</p>
          </section>
          <section class="comic-pane-group">
            <span class="section-kicker">连续性资产</span>
            <p class="pane-note">当前流程尚未建立可锁定的角色、场景与风格资产。不要将单次提示词视为已锁定资产。</p>
          </section>
          <section v-if="externalWait" class="comic-pane-group" role="status">
            <p class="pane-note">{{ externalWait === 'needs_reconciliation' ? '原任务缺少供应商任务 ID，需要人工查账；系统不会重新付费提交。' : '供应商任务尚未完成；继续查询沿用原任务 ID。' }}</p>
            <button v-if="externalWait === 'external_job_pending'" class="ui-button sm" :disabled="resuming" @click="queryOriginalJob">{{ resuming ? '查询中…' : '继续查询原任务' }}</button>
            <p v-if="resumeError" class="pane-note" role="alert">{{ resumeError }}</p>
          </section>
        </aside>
        <aside v-else class="workspace-pane workflow-pane">
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
        <div v-if="isComic" class="workspace-pane comic-workspace-center">
          <Splitpanes horizontal class="comic-center-splitpanes">
            <Pane :size="57" :min-size="28">
              <section class="comic-canvas" aria-label="当前创作画布">
                <header class="comic-canvas-toolbar"><div><span class="section-kicker">WORK CANVAS</span><h1>{{ run ? `镜头 ${shotNumber ?? '—'}` : '漫剧制作台' }}</h1></div><span class="comic-canvas-status">{{ currentStage }}</span></header>
                <div class="comic-stage">
                  <template v-if="previewUrl && selectedArtifact?.type === 'video'"><video :src="previewUrl" controls preload="metadata" aria-label="当前视频产物" /></template>
                  <img v-else-if="previewUrl" :src="previewUrl" alt="当前镜头的真实图片产物" />
                  <div v-else class="comic-stage-empty"><Layers3 :size="27" :stroke-width="1.4" /><strong>{{ previewLoading ? '正在载入真实产物…' : run?.state.image_path ? '图片暂时无法载入' : '当前镜头尚无画面' }}</strong><span>{{ run ? '已生成的画面会自动出现在这里' : '从下方 AI 导演助手开始，创意将逐步变成制作结果' }}</span></div>
                </div>
                <div class="comic-next-step"><span>下一步</span><p>{{ nextAction }}</p></div>
              </section>
            </Pane>
            <Pane :size="43" :min-size="25">
              <div class="comic-director-dock"><HomeView :key="domain" :domain="domain" :embedded="true" :initial-run-id="runId" :runs="run ? [run] : []" :approvals="[]" @refresh="refresh" @run-created="(created) => navigate({ name: 'workspace_run', domain, runId: created.id })" @chatting="(active) => (chatActive = active)" /></div>
            </Pane>
          </Splitpanes>
        </div>
        <div v-else class="workspace-pane canvas-pane">
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
        <aside v-if="isComic" class="workspace-pane workspace-inspector comic-inspector">
          <header class="pane-heading"><span>Inspector</span><strong>当前镜头</strong></header>
          <section class="comic-pane-group"><span class="section-kicker">制作阶段</span><WorkflowProgress :steps="steps" compact /></section>
          <section v-if="run" class="comic-pane-group">
            <span class="section-kicker">创作要求 · 来自当前任务</span>
            <p class="comic-inspector-prompt">{{ shotPrompt || '当前任务未保存创作描述。' }}</p>
            <dl class="inspector-meta"><div><dt>镜头</dt><dd>{{ shotNumber ?? '—' }}</dd></div><div><dt>当前风格</dt><dd>{{ String(run.state.visual_style ?? '未设定') }}</dd></div><div><dt>状态</dt><dd>{{ statusText }}</dd></div><div><dt>已发生费用</dt><dd>¥{{ (run.cost_fen / 100).toFixed(2) }}</dd></div></dl>
          </section>
          <section v-if="run?.state.qc_result" class="comic-pane-group"><span class="section-kicker">视觉检查</span><strong class="comic-qc-result">{{ run.state.qc_passed ? '预筛通过' : '需要复核' }}</strong><p class="pane-note">{{ String((run.state.qc_result as Record<string, unknown>).reason ?? '暂无说明') }}</p></section>
          <section v-if="selectedArtifact" class="comic-pane-group"><span class="section-kicker">选中产物</span><p class="comic-selected-name">{{ presenter.artifactName(selectedArtifact.source, selectedArtifact.type) }}</p><dl class="inspector-meta"><div><dt>类型</dt><dd>{{ selectedArtifact.type }}</dd></div><div><dt>版本</dt><dd>{{ selectedArtifact.version }}</dd></div></dl></section>
          <section v-if="run" class="comic-pane-group"><button class="text-action" @click="navigate({ name: 'task_run', runId: run.id })">查看完整任务记录 →</button></section>
        </aside>
        <aside v-else class="workspace-pane workspace-inspector">
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
