<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Pane, Splitpanes } from 'splitpanes'
import 'splitpanes/dist/splitpanes.css'
import WorkflowProgress, { type WorkflowStep } from '../components/WorkflowProgress.vue'
import { presenterFor } from '../domains/presenters'
import { navigate } from '../router'
import { getArtifacts, getEvents, getRun, resumeRun, subscribeRunEvents, type RuntimeEvent } from '../services/core'
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
let unsubscribe: (() => void) | null = null

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
  if (props.runId) {
    await refresh()
    follow()
    return
  }
  run.value = null
  artifacts.value = []
  events.value = []
}, { immediate: true })
onBeforeUnmount(() => unsubscribe?.())
</script>

<template>
  <section class="domain-workspace">
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
