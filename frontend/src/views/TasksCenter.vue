<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import ApprovalCard from '../components/approval/ApprovalCard.vue'
import StatusBadge from '../components/StatusBadge.vue'
import EmptyState from '../components/EmptyState.vue'
import { presenterFor } from '../domains/presenters'
import { navigate } from '../router'
import {
  cancelBatch, cancelRun, createBatch, decideApproval, getApprovals, getBatches, getRuns,
} from '../services/core'
import type { CoreApproval, CoreBatch, CoreRun } from '../types'

/* 任务中心是高级管理视图；普通用户在 Domain Workspace 里完成整个流程。 */

const props = defineProps<{ tab?: string; focusRunId?: string; focusBatchId?: string }>()

const tabs = [
  { id: 'running', label: '运行中' },
  { id: 'waiting', label: '等待决策' },
  { id: 'batches', label: '批次' },
  { id: 'failed', label: '失败' },
  { id: 'history', label: '历史' },
]
const active = ref(props.tab && tabs.some((item) => item.id === props.tab) ? props.tab : 'running')
const runs = ref<CoreRun[]>([])
const approvals = ref<CoreApproval[]>([])
const batches = ref<CoreBatch[]>([])
const busy = ref(false)
const error = ref('')
const creating = ref(false)
let timer: number | undefined

const pending = computed(() => approvals.value.filter((item) => item.decision === 'pending'))
const liveRuns = computed(() => runs.value.filter((item) => ['pending', 'running', 'waiting'].includes(item.status)))
const failedRuns = computed(() => runs.value.filter((item) => item.status === 'failed'))
const historyRuns = computed(() => runs.value.filter((item) => ['completed', 'cancelled'].includes(item.status)))
const focusRun = computed(() => runs.value.find((item) => item.id === props.focusRunId) ?? null)
const focusBatch = computed(() => batches.value.find((item) => item.id === props.focusBatchId) ?? null)

async function refresh(): Promise<void> {
  const [runData, approvalData, batchData] = await Promise.all([getRuns(), getApprovals(), getBatches()])
  if (runData) runs.value = runData
  if (approvalData) approvals.value = approvalData
  if (batchData) batches.value = batchData
}

async function decide(approval: CoreApproval, action: 'approve' | 'reject' | 'revise', response: Record<string, unknown>): Promise<void> {
  if (busy.value || approval.decision !== 'pending') return
  busy.value = true
  try {
    await decideApproval(approval.id, action, response)
    await refresh()
  } catch (taskError) {
    error.value = taskError instanceof Error ? taskError.message : '审批失败'
  } finally {
    busy.value = false
  }
}

async function cancelOne(run: CoreRun): Promise<void> {
  busy.value = true
  try { await cancelRun(run.id); await refresh() }
  catch (taskError) { error.value = taskError instanceof Error ? taskError.message : '取消失败' }
  finally { busy.value = false }
}

async function cancelOneBatch(batch: CoreBatch): Promise<void> {
  busy.value = true
  try { await cancelBatch(batch.id); await refresh() }
  catch (taskError) { error.value = taskError instanceof Error ? taskError.message : '取消失败' }
  finally { busy.value = false }
}

async function createDemo(): Promise<void> {
  creating.value = true
  try {
    await createBatch({
      name: 'Commerce 5 Run Demo',
      workflow: 'commerce.production.v1',
      concurrency_limit: 2,
      items: ['便携阅读灯', '保温水杯', '桌面收纳架', '旅行颈枕', '无线鼠标'].map((requirement) => ({ requirement, locale: 'ru-RU' })),
    })
    active.value = 'batches'
    await refresh()
  } catch (taskError) {
    error.value = taskError instanceof Error ? taskError.message : 'Demo 创建失败'
  } finally {
    creating.value = false
  }
}

function runTitle(run: CoreRun): string {
  const state = run.state as Record<string, unknown>
  return String(state.requirement ?? state.prompt ?? run.workflow)
}

function money(fen: number): string {
  return `¥${(fen / 100).toFixed(2)}`
}

/* Batch 状态是只读 Badge；取消是独立 Action，两者不共用 UI 元素。 */
function batchCanCancel(batch: CoreBatch): boolean {
  return ['pending', 'running', 'waiting'].includes(batch.status)
}

function batchCount(batch: CoreBatch, group: 'completed' | 'waiting' | 'failed' | 'cancelled'): number {
  if (batch.progress) return batch.progress[group]
  if (group === 'waiting') return batch.runs.filter((run) => ['pending', 'running', 'waiting'].includes(run.status)).length
  return batch.runs.filter((run) => run.status === group).length
}

function batchPercent(batch: CoreBatch): number {
  return batch.progress?.percent ?? 0
}

onMounted(() => {
  void refresh()
  timer = window.setInterval(() => void refresh(), 2500)
})
onBeforeUnmount(() => { if (timer !== undefined) window.clearInterval(timer) })
</script>

<template>
  <main class="page tasks-page">
    <header class="page-header">
      <div>
        <span class="section-kicker">高级管理视图</span>
        <h1>任务中心</h1>
        <p>Run、Batch 与 Approval 的统一管理入口；普通流程请在 Domain Workspace 内完成。</p>
      </div>
      <div class="page-actions">
        <button class="ui-button" :disabled="creating" @click="createDemo">
          {{ creating ? '创建中……' : '创建 5 商品 Mock Demo' }}
        </button>
      </div>
    </header>

    <nav class="detail-tabs" aria-label="任务视图">
      <button v-for="item in tabs" :key="item.id" :class="{ active: active === item.id }" @click="active = item.id">
        {{ item.label }}
      </button>
    </nav>

    <p v-if="error" class="error-box" role="alert">{{ error }}</p>

    <section v-if="focusRun" class="surface task-focus">
      <header class="panel-header">
        <div><span class="section-kicker">{{ focusRun.domain }} Run</span><h2>{{ runTitle(focusRun) }}</h2></div>
        <StatusBadge :status="focusRun.status" />
      </header>
      <dl class="inspector-meta">
        <div><dt>工作流</dt><dd>{{ focusRun.workflow }}</dd></div>
        <div><dt>当前节点</dt><dd>{{ focusRun.current_node || '—' }}</dd></div>
        <div><dt>费用</dt><dd>{{ money(focusRun.cost_fen) }}</dd></div>
      </dl>
      <footer>
        <button class="text-action" @click="navigate({ name: 'workspace_run', domain: focusRun.domain, runId: focusRun.id })">在 Workspace 打开 →</button>
        <button v-if="['pending','running','waiting'].includes(focusRun.status)" class="ui-button sm danger" :disabled="busy" @click="cancelOne(focusRun)">取消 Run</button>
      </footer>
    </section>

    <section v-else-if="focusBatch" class="surface task-focus">
      <header class="panel-header">
        <div><span class="section-kicker">Batch</span><h2>{{ focusBatch.name }}</h2></div>
        <div class="batch-head-state">
          <StatusBadge :status="focusBatch.status" />
          <button v-if="batchCanCancel(focusBatch)" class="ui-button sm danger" :disabled="busy" @click="cancelOneBatch(focusBatch)">取消批次</button>
        </div>
      </header>
      <dl class="inspector-meta">
        <div><dt>并发</dt><dd>{{ focusBatch.concurrency_limit }}</dd></div>
        <div><dt>完成</dt><dd>{{ batchCount(focusBatch, 'completed') }}</dd></div>
        <div><dt>等待</dt><dd>{{ batchCount(focusBatch, 'waiting') }}</dd></div>
        <div><dt>失败</dt><dd>{{ batchCount(focusBatch, 'failed') }}</dd></div>
        <div><dt>取消</dt><dd>{{ batchCount(focusBatch, 'cancelled') }}</dd></div>
      </dl>
      <div class="data-table-wrap"><table class="data-table">
        <thead><tr><th>任务</th><th>当前节点</th><th>状态</th><th>费用</th><th></th></tr></thead>
        <tbody>
          <tr v-for="run in focusBatch.runs" :key="run.id">
            <td><strong>{{ runTitle(run) }}</strong><small>{{ run.id.slice(0, 16) }}</small></td>
            <td>{{ presenterFor(run.domain).nodeLabel(run.current_node || run.workflow) }}</td>
            <td><StatusBadge :status="run.status" /></td>
            <td>{{ money(run.cost_fen) }}</td>
            <td><button class="text-action" @click="navigate({ name: 'workspace_run', domain: run.domain, runId: run.id })">打开</button></td>
          </tr>
        </tbody>
      </table></div>
    </section>

    <section v-else-if="active === 'running'" class="surface task-table-surface">
      <div v-if="liveRuns.length" class="data-table-wrap"><table class="data-table task-data-table">
        <thead><tr><th>任务</th><th>Domain</th><th>当前步骤</th><th>状态</th><th>费用</th><th></th></tr></thead>
        <tbody><tr v-for="run in liveRuns" :key="run.id">
          <td><strong>{{ runTitle(run) }}</strong><small>{{ run.id.slice(0, 16) }}</small></td>
          <td>{{ presenterFor(run.domain).label }}</td>
          <td>{{ presenterFor(run.domain).nodeLabel(run.current_node || '') || run.workflow }}</td>
          <td><StatusBadge :status="run.status" /></td><td>{{ money(run.cost_fen) }}</td>
          <td class="table-actions"><button class="text-action" @click="navigate({ name: 'workspace_run', domain: run.domain, runId: run.id })">打开</button><button class="text-action danger-text" :disabled="busy" @click="cancelOne(run)">取消</button></td>
        </tr></tbody>
      </table></div>
      <EmptyState v-if="!liveRuns.length" title="没有运行中的任务" description="在专业创作域提交工作流后，任务会出现在这里。" />
    </section>

    <section v-else-if="active === 'waiting'" class="task-list">
      <ApprovalCard
        v-for="item in pending"
        :key="item.id"
        :approval="item"
        :domain="runs.find((run) => run.id === item.run_id)?.domain ?? 'studio'"
        :busy="busy"
        @decide="(action, response) => decide(item, action, response)"
      />
      <EmptyState v-if="!pending.length" title="没有待决策任务" description="Workflow 需要人工确认时会自动出现在这里和 Workspace 会话里。" />
    </section>

    <section v-else-if="active === 'batches'" class="surface task-table-surface">
      <div v-if="batches.length" class="data-table-wrap"><table class="data-table batch-data-table">
        <thead><tr><th>Batch</th><th>进度</th><th>完成 / 等待 / 失败</th><th>并发</th><th>费用</th><th>状态</th><th></th></tr></thead>
        <tbody><tr v-for="batch in batches" :key="batch.id">
          <td><strong>{{ batch.name }}</strong><small>v{{ batch.version }} · {{ batch.id.slice(0, 13) }}</small></td>
          <td><div class="table-progress"><i><b :style="{ width: `${batchPercent(batch)}%` }"></b></i><span>{{ batchPercent(batch) }}%</span></div></td>
          <td>{{ batchCount(batch, 'completed') }} / {{ batchCount(batch, 'waiting') }} / {{ batchCount(batch, 'failed') }}</td>
          <td>{{ batch.concurrency_limit }}</td><td>{{ money(batch.progress?.cost_fen ?? 0) }}</td><td><StatusBadge :status="batch.status" /></td>
          <td class="table-actions"><button class="text-action" @click="navigate({ name: 'task_batch', batchId: batch.id })">详情</button><button v-if="batchCanCancel(batch)" class="text-action danger-text" :disabled="busy" @click="cancelOneBatch(batch)">取消</button></td>
        </tr></tbody>
      </table></div>
      <EmptyState v-if="!batches.length" title="暂无批次" description="创建 Mock Demo 后会运行 5 个真实 Commerce Run。" />
    </section>

    <section v-else class="surface task-table-surface">
      <div v-if="(active === 'failed' ? failedRuns : historyRuns).length" class="data-table-wrap"><table class="data-table task-data-table">
        <thead><tr><th>任务</th><th>Domain</th><th>最后步骤</th><th>状态</th><th>费用</th><th>更新时间</th><th></th></tr></thead>
        <tbody><tr v-for="run in active === 'failed' ? failedRuns : historyRuns" :key="run.id">
          <td><strong>{{ runTitle(run) }}</strong><small>{{ run.id.slice(0, 16) }}</small></td><td>{{ presenterFor(run.domain).label }}</td>
          <td>{{ presenterFor(run.domain).nodeLabel(run.current_node || '') || '—' }}</td><td><StatusBadge :status="run.status" /></td><td>{{ money(run.cost_fen) }}</td>
          <td>{{ new Date(run.updated_at).toLocaleString() }}</td><td><button class="text-action" @click="navigate({ name: 'workspace_run', domain: run.domain, runId: run.id })">打开</button></td>
        </tr></tbody>
      </table></div>
      <EmptyState v-if="!(active === 'failed' ? failedRuns : historyRuns).length" title="暂无记录" description="任务完成或失败后会进入历史。" />
    </section>
  </main>
</template>
