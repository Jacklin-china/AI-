<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowDown } from 'lucide-vue-next'
import type { ConversationMessage, CoreRun } from '../../types'
import type { RuntimeEvent } from '../../services/core'
import AssistantMessageBlock from './AssistantMessageBlock.vue'
import AssistantStreamingBlock from './AssistantStreamingBlock.vue'
import ErrorRecoveryPanel from './ErrorRecoveryPanel.vue'
import UserMessageBubble from './UserMessageBubble.vue'
import WorkflowActivity from './WorkflowActivity.vue'
const props = defineProps<{ messages: ConversationMessage[]; streaming: string; run: CoreRun | null; activities?: RuntimeEvent[]; error: string; emptyHint?: string; examples?: string[] }>()
defineEmits<{ retry: []; openRun: [run: CoreRun]; example: [text: string] }>()
const scroller = ref<HTMLElement | null>(null)
const atBottom = ref(true)
function check(): void { const el = scroller.value; if (el) atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 72 }
function bottom(): void { scroller.value?.scrollTo({ top: scroller.value.scrollHeight, behavior: 'smooth' }) }

const displayedStreaming = ref('')
let pendingChars = ''
let consumed = 0
let drainTimer: number | null = null

function drain(): void {
  if (!pendingChars) { drainTimer = null; return }
  const step = Math.max(1, Math.ceil(pendingChars.length / 24))
  displayedStreaming.value += pendingChars.slice(0, step)
  pendingChars = pendingChars.slice(step)
  drainTimer = window.setTimeout(drain, 24)
}

watch(() => props.streaming, (value) => {
  if (value.length <= consumed) {
    consumed = 0
    pendingChars = ''
    displayedStreaming.value = ''
    if (drainTimer !== null) { window.clearTimeout(drainTimer); drainTimer = null }
    if (!value) return
  }
  pendingChars += value.slice(consumed)
  consumed = value.length
  if (drainTimer === null) drainTimer = window.setTimeout(drain, 24)
})

let prevMessageCount = 0
watch(() => [props.messages.length, displayedStreaming.value, props.run?.status, props.activities?.length], async () => {
  const count = props.messages.length
  const ownMessageSent = count !== prevMessageCount && props.messages[count - 1]?.role === 'user'
  prevMessageCount = count
  const follow = atBottom.value
  await nextTick()
  if (ownMessageSent || follow) bottom()
})
onMounted(check)
onBeforeUnmount(() => { if (drainTimer !== null) window.clearTimeout(drainTimer) })

const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled']
const activityAnchor = ref<number | null>(null)
const activityExpanded = ref(false)

watch(() => props.run?.id, () => {
  activityAnchor.value = null
  activityExpanded.value = false
  const status = props.run?.status
  if (status && TERMINAL_STATUSES.includes(status)) activityAnchor.value = props.messages.length
})
watch(() => props.run?.status, (status) => {
  if (status && TERMINAL_STATUSES.includes(status) && activityAnchor.value === null) {
    activityAnchor.value = props.messages.length
  }
})

/* 同一节点只保留最新状态，run 级事件各留一条，避免时间线无限堆叠。 */
const activityLines = computed<RuntimeEvent[]>(() => {
  const events = [...(props.activities ?? [])].sort((a, b) => a.sequence - b.sequence)
  const started: RuntimeEvent[] = []
  const ended: RuntimeEvent[] = []
  let cost: RuntimeEvent | null = null
  const byNode = new Map<string, RuntimeEvent>()
  const rest: RuntimeEvent[] = []
  for (const event of events) {
    if (event.event_type === 'run_started') { started[0] = event; continue }
    if (['run_completed', 'run_failed', 'run_cancelled'].includes(event.event_type)) { ended[0] = event; continue }
    if (event.event_type === 'cost_updated') { cost = event; continue }
    if (event.node_id) { byNode.set(event.node_id, event); continue }
    rest.push(event)
  }
  return [...started, ...byNode.values(), ...rest, ...(cost ? [cost] : []), ...ended]
})

const runSummary = computed(() => {
  const status = props.run?.status
  if (status === 'completed') return '任务完成'
  if (status === 'failed') return '任务失败'
  if (status === 'cancelled') return '任务已取消'
  return '任务进度'
})

function activityText(event: RuntimeEvent): string {
  if (event.event_type === 'run_waiting') {
    const kind = String(event.payload.kind ?? '')
    if (kind === 'external_job_pending') return '供应商仍在生成，可继续查询原任务'
    if (kind === 'needs_reconciliation') return '缺少供应商任务 ID，等待人工查账'
  }
  const labels: Record<string, string> = {
    run_started: '任务开始', node_started: '开始执行', node_progress: '正在执行',
    node_completed: '已完成', node_retrying: '正在重试', node_failed: '执行失败',
    artifact_created: '产物已保存', approval_required: '需要你确认',
    approval_resolved: '已记录你的决定', cost_updated: '费用已更新',
    run_completed: '任务完成', run_failed: '任务失败', run_waiting: '等待确认',
  }
  return `${event.node_id ? `${event.node_id} · ` : ''}${labels[event.event_type] ?? event.event_type}`
}
</script>
<template>
  <div ref="scroller" class="message-scroller" @scroll="check">
    <div class="message-list">
      <template v-if="!messages.length && !streaming && !activities?.length && !run && !error">
        <p class="list-empty-hint">{{ emptyHint ?? '从一个问题开始，也可以直接描述你要制作的内容。' }}</p>
        <div v-if="examples?.length" class="list-examples">
          <span>可以试试：</span>
          <button v-for="item in examples" :key="item" type="button" @click="$emit('example', item)">{{ item }}</button>
        </div>
      </template>
      <template v-for="(message, index) in messages" :key="message.id">
        <UserMessageBubble v-if="message.role === 'user'" :content="message.content" />
        <AssistantMessageBlock v-else-if="message.role === 'assistant'" :content="message.content" />
        <div v-if="activityAnchor === index + 1 && activityLines.length" class="runtime-activity-list anchored" aria-label="任务进度">
          <button type="button" class="activity-toggle" :data-status="run?.status" @click="activityExpanded = !activityExpanded">
            <span>{{ runSummary }}</span>
            <small>{{ activityExpanded ? '收起执行明细' : `查看执行明细（${activities?.length ?? 0}）` }}</small>
          </button>
          <template v-if="activityExpanded">
            <div v-for="event in activityLines" :key="event.id" class="runtime-activity" :data-event="event.event_type">
              <span>{{ ['node_completed', 'run_completed', 'artifact_created'].includes(event.event_type) ? '✓' : event.event_type.includes('failed') ? '!' : '●' }}</span>
              <span>{{ activityText(event) }}</span>
            </div>
          </template>
        </div>
      </template>
      <AssistantStreamingBlock v-if="streaming || displayedStreaming" :content="displayedStreaming" />
      <div v-if="activityAnchor === null && activityLines.length" class="runtime-activity-list" aria-label="任务进度">
        <div v-for="event in activityLines" :key="event.id" class="runtime-activity" :data-event="event.event_type">
          <span>{{ ['node_completed', 'run_completed', 'artifact_created'].includes(event.event_type) ? '✓' : event.event_type.includes('failed') ? '!' : '●' }}</span>
          <span>{{ activityText(event) }}</span>
        </div>
      </div>
      <WorkflowActivity v-if="run" :run="run" @open="$emit('openRun', $event)" />
      <ErrorRecoveryPanel v-if="error" :message="error" @retry="$emit('retry')" />
    </div>
    <button v-if="!atBottom" type="button" class="back-bottom" @click="bottom"><ArrowDown :size="14" />回到底部</button>
  </div>
</template>
