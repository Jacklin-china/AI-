<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ArrowDown, Download, RotateCcw, X } from 'lucide-vue-next'
import type { ConversationMessage, CoreApproval, CoreArtifact, CoreRun, MediaJob } from '../../types'
import { presenterFor } from '../../domains/presenters'
import type { RuntimeEvent } from '../../services/core'
import ApprovalCard from '../approval/ApprovalCard.vue'
import AssistantMessageBlock from './AssistantMessageBlock.vue'
import AssistantStreamingBlock from './AssistantStreamingBlock.vue'
import ChatImageAttachment from './ChatImageAttachment.vue'
import ErrorRecoveryPanel from './ErrorRecoveryPanel.vue'
import UserMessageBubble from './UserMessageBubble.vue'
import WorkflowActivity from './WorkflowActivity.vue'
interface InlineRunState {
  run: CoreRun
  activities: RuntimeEvent[]
  approval: CoreApproval | null
  artifact: CoreArtifact | null
  imageUrl: string
  videoUrl: string
}
const props = defineProps<{
  messages: ConversationMessage[]; streaming: string; run: CoreRun | null
  activities?: RuntimeEvent[]; imageUrl?: string
  approvalBusy?: boolean; homeMode?: boolean
  streamingByMessage?: Record<string, string>; errorMessageId?: string
  pendingMessages?: Record<string, 'queued' | 'replying' | 'failed'>
  activityByMessage?: Record<string, string>
  imagePhases?: Record<string, { status: 'prepared' | 'generating' | 'ready' | 'summarized' | 'failed'; width: number; height: number }>
  messageMedia?: Record<string, { type: 'image' | 'video'; url: string; filename: string }>
  messageMediaErrors?: Record<string, boolean>
  mediaJobs?: MediaJob[]
  inlineRuns?: Record<string, InlineRunState>
  error: string; emptyHint?: string; examples?: string[]
}>()
defineEmits<{
  retry: []; openRun: [run: CoreRun]; example: [text: string]
  decideInline: [messageId: string, action: 'approve' | 'reject' | 'revise', response: Record<string, unknown>]
  decideMedia: [generationRequestId: string, decision: 'approve' | 'reject']
}>()
const scroller = ref<HTMLElement | null>(null)
const previewDialog = ref<HTMLDialogElement | null>(null)
const previewImage = ref<{ url: string; filename: string } | null>(null)
const previewStage = ref<HTMLElement | null>(null)
const previewElement = ref<HTMLImageElement | null>(null)
const previewScale = ref(1)
const previewX = ref(0)
const previewY = ref(0)
const dragging = ref(false)
let dragStart = { x: 0, y: 0, offsetX: 0, offsetY: 0 }
const atBottom = ref(true)
async function openImage(media: { url: string; filename: string }): Promise<void> {
  resetPreview()
  previewImage.value = media
  await nextTick()
  previewDialog.value?.showModal()
}
function resetPreview(): void {
  previewScale.value = 1
  previewX.value = 0
  previewY.value = 0
  dragging.value = false
}
function closeImage(): void { previewDialog.value?.close(); previewImage.value = null; resetPreview() }
function boundPan(): void {
  const image = previewElement.value
  const stage = previewStage.value
  if (!image || !stage || previewScale.value <= 1) {
    previewX.value = 0
    previewY.value = 0
    return
  }
  const maxX = Math.max(0, (image.offsetWidth * previewScale.value - stage.clientWidth) / 2)
  const maxY = Math.max(0, (image.offsetHeight * previewScale.value - stage.clientHeight) / 2)
  previewX.value = Math.max(-maxX, Math.min(maxX, previewX.value))
  previewY.value = Math.max(-maxY, Math.min(maxY, previewY.value))
}
function zoomPreview(event: WheelEvent): void {
  const next = Math.max(1, Math.min(5, previewScale.value * (event.deltaY < 0 ? 1.15 : 1 / 1.15)))
  previewScale.value = Math.abs(next - 1) < 0.01 ? 1 : next
  boundPan()
}
function beginDrag(event: PointerEvent): void {
  if (event.button !== 0 || previewScale.value <= 1) return
  dragging.value = true
  dragStart = { x: event.clientX, y: event.clientY, offsetX: previewX.value, offsetY: previewY.value }
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  event.preventDefault()
}
function moveDrag(event: PointerEvent): void {
  if (!dragging.value) return
  previewX.value = dragStart.offsetX + event.clientX - dragStart.x
  previewY.value = dragStart.offsetY + event.clientY - dragStart.y
  boundPan()
}
function endDrag(event: PointerEvent): void {
  dragging.value = false
  const target = event.currentTarget as HTMLElement
  if (target.hasPointerCapture(event.pointerId)) target.releasePointerCapture(event.pointerId)
}
watch(() => props.messageMedia, (media) => {
  if (previewImage.value && !Object.values(media ?? {}).some((item) => item.url === previewImage.value?.url)) closeImage()
})
function check(): void { const el = scroller.value; if (el) atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 72 }
function bottom(smooth = false): void { scroller.value?.scrollTo({ top: scroller.value.scrollHeight, behavior: smooth ? 'smooth' : 'auto' }) }

let prevMessageCount = 0
watch(() => [props.messages.length, props.streaming, props.streamingByMessage, props.run?.status, props.activities?.length, props.imageUrl, props.inlineRuns, props.imagePhases, props.messageMedia], async () => {
  const count = props.messages.length
  const ownMessageSent = count !== prevMessageCount && props.messages[count - 1]?.role === 'user'
  prevMessageCount = count
  const follow = atBottom.value
  await nextTick()
  if (ownMessageSent || follow) bottom()
})
onMounted(check)

function imageRequestId(message: ConversationMessage): string | null {
  return message.event_id?.startsWith('generation-prompt:')
    ? message.event_id.slice('generation-prompt:'.length) : null
}

function imageResult(requestId: string): ConversationMessage | undefined {
  return props.messages.find((item) => item.event_id === `generation-artifact:${requestId}`)
}

function mediaJobForUser(message: ConversationMessage): MediaJob | undefined {
  const requestId = message.event_id?.startsWith('generation-user:')
    ? message.event_id.slice('generation-user:'.length) : null
  return requestId ? props.mediaJobs?.find((job) => job.generation_request_id === requestId) : undefined
}

function awaitingCost(requestId: string): MediaJob | undefined {
  return props.mediaJobs?.find((job) =>
    job.generation_request_id === requestId && job.approval_status === 'pending')
}

function hasImagePrompt(requestId: string): boolean {
  return props.messages.some((item) => item.event_id === `generation-prompt:${requestId}`)
}

function hasFailureMessage(requestId: string): boolean {
  return props.messages.some((item) => item.event_id?.startsWith(`generation-error:${requestId}`)
    || item.event_id?.startsWith(`generation-status:${requestId}`))
}

function isHomeImageArtifact(message: ConversationMessage): boolean {
  return message.event_id?.startsWith('generation-artifact:') ?? false
}

function imagePlaceholderRatio(requestId: string): string {
  const phase = props.imagePhases?.[requestId]
  const ratio = phase && phase.height > 0 ? phase.width / phase.height : 1
  return String(Math.max(0.75, Math.min(ratio, 2)))
}

function homeStatus(run: CoreRun, approval: CoreApproval | null): string {
  if (run.status === 'completed') return run.domain === 'commerce' && run.state.execution_mode === 'fast' ? '模拟电商草稿已完成' : '已完成'
  if (run.status === 'failed') return '这次没有完成'
  if (run.status === 'cancelled') return '已取消'
  const approvalKind = String(approval?.request.kind ?? '')
  if (run.status === 'waiting' && approvalKind === 'cost_approval') return '等待费用确认'
  if (run.status === 'waiting' && approvalKind === 'creative_review') return '画面已生成，等待你审核'
  if (run.status === 'waiting') return '等待你确认'
  const labels: Record<string, string> = {
    prepare: '正在准备画面', generate: '正在生成图片', video: '正在生成视频',
    qc: '正在检查画面', archive: '正在保存结果', rework: '正在修改画面',
    requirement: '正在理解需求', source_search: '正在收集资料',
    asset_generation: '正在制作素材', localize: '正在整理内容',
  }
  return labels[run.current_node] ?? '正在处理'
}

function fastCommerceSummary(run: CoreRun): string {
  if (run.domain !== 'commerce' || run.state.execution_mode !== 'fast' || run.status !== 'completed') return ''
  const listing = run.state.localized_listing
  if (!listing || typeof listing !== 'object' || Array.isArray(listing)) return '模拟电商任务已完成；产物保存在当前任务中。'
  const draft = listing as Record<string, unknown>
  const title = typeof draft.title === 'string' ? draft.title : '商品 Listing'
  return `Mock 数据 · ${title}。已完成选品、定价、Listing、素材与 QC；Marketplace 草稿也是模拟结果。`
}

function homeError(state: InlineRunState): string {
  const failure = [...state.activities].reverse().find((event) =>
    event.event_type === 'run_failed' || event.event_type === 'node_failed',
  )
  if (!failure) return state.run.status === 'failed' ? '生成未完成，请稍后查看错误记录。' : ''
  const message = String(failure.payload.safe_message ?? '生成未完成，请稍后查看错误记录。')
  const errorId = failure.payload.error_id ? ` · 错误编号：${String(failure.payload.error_id)}` : ''
  return `${message}${errorId}`
}

function qcSummary(run: CoreRun): string {
  const result = run.state?.qc_result
  if (!result || typeof result !== 'object' || Array.isArray(result)) return ''
  const qc = result as Record<string, unknown>
  if (typeof qc.reason === 'string' && qc.reason.trim()) return `画面检查：${qc.reason.trim()}`
  if (qc.composition_ok === true && qc.broken_hands === false && qc.watermark === false) return '画面已通过预筛，等待你确认。'
  return '画面检查已完成，请确认结果。'
}

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
  const nodeName = event.node_id ? `${props.run ? presenterFor(props.run.domain).nodeLabel(event.node_id) : event.node_id} · ` : ''
  const base = labels[event.event_type] ?? event.event_type
  if (event.event_type === 'node_failed' || event.event_type === 'run_failed') {
    const reason = String(event.payload.safe_message ?? '').trim()
    if (reason) return `${nodeName}${base}：${reason}`
  }
  return `${nodeName}${base}`
}
</script>
<template>
  <div ref="scroller" class="message-scroller" :class="{ 'home-message-scroller': homeMode }" @scroll="check">
    <div class="message-list">
      <template v-if="!messages.length && !streaming && !activities?.length && !run && !error">
        <p class="list-empty-hint">{{ emptyHint ?? '从一个问题开始，也可以直接描述你要制作的内容。' }}</p>
        <div v-if="examples?.length" class="list-examples">
          <span>可以试试：</span>
          <button v-for="item in examples" :key="item" type="button" @click="$emit('example', item)">{{ item }}</button>
        </div>
      </template>
      <template v-for="(message, index) in messages" :key="message.id">
        <UserMessageBubble v-if="message.role === 'user'" :content="message.content" :pending="homeMode && !activityByMessage?.[message.id] && !imagePhases?.[message.id] ? pendingMessages?.[message.id] : undefined" />
        <div v-if="homeMode && mediaJobForUser(message) && !hasImagePrompt(mediaJobForUser(message)!.generation_request_id)" class="chat-image-generation" aria-live="polite">
          <div v-if="awaitingCost(mediaJobForUser(message)!.generation_request_id)" class="chat-cost-card">
            <strong>本次生图需要确认费用</strong>
            <p>预计 ¥{{ ((mediaJobForUser(message)!.estimate_fen ?? 0) / 100).toFixed(2) }}。确认后才会开始生成；取消则不会提交付费任务。</p>
            <div><button type="button" :disabled="approvalBusy" @click="$emit('decideMedia', mediaJobForUser(message)!.generation_request_id, 'reject')">取消</button><button type="button" :disabled="approvalBusy" class="primary" @click="$emit('decideMedia', mediaJobForUser(message)!.generation_request_id, 'approve')">确认并生成</button></div>
          </div>
          <template v-else-if="['pending', 'generating'].includes(mediaJobForUser(message)!.status)">
            <div class="chat-image-wave" role="status" :aria-label="mediaJobForUser(message)!.status === 'generating' ? '正在生图' : '正在准备图片'"><span v-for="(character, position) in (mediaJobForUser(message)!.status === 'generating' ? '正在生图' : '正在准备图片')" :key="position" :style="{ animationDelay: `${position * 0.12}s` }" aria-hidden="true">{{ character }}</span></div>
            <div class="chat-image-skeleton" role="img" aria-label="图片生成中"></div>
          </template>
          <p v-else-if="mediaJobForUser(message)!.status === 'failed' && !hasFailureMessage(mediaJobForUser(message)!.generation_request_id)" class="chat-inline-error" role="alert">{{ mediaJobForUser(message)!.error_message }}</p>
        </div>
        <AssistantMessageBlock v-else-if="message.role === 'assistant' && !(homeMode && isHomeImageArtifact(message))" :content="message.content" :show-mark="!homeMode" />
        <div v-if="homeMode && imageRequestId(message)" class="chat-image-generation" aria-live="polite">
          <div v-if="awaitingCost(imageRequestId(message)!)" class="chat-cost-card">
            <strong>本次生图需要确认费用</strong>
            <p>预计 ¥{{ ((awaitingCost(imageRequestId(message)!)!.estimate_fen ?? 0) / 100).toFixed(2) }}。确认后才会开始生成；取消则不会提交付费任务。</p>
            <div><button type="button" :disabled="approvalBusy" @click="$emit('decideMedia', imageRequestId(message)!, 'reject')">取消</button><button type="button" :disabled="approvalBusy" class="primary" @click="$emit('decideMedia', imageRequestId(message)!, 'approve')">确认并生成</button></div>
          </div>
          <template v-if="imageResult(imageRequestId(message)!)?.artifact_id">
            <ChatImageAttachment v-if="messageMedia?.[imageResult(imageRequestId(message)!)!.id]" :media="messageMedia[imageResult(imageRequestId(message)!)!.id]" @open="openImage" />
            <p v-else-if="messageMediaErrors?.[imageResult(imageRequestId(message)!)!.id]" class="chat-image-load-error" role="alert">图片已保存，但预览暂时无法加载。</p>
            <div v-else class="chat-image-skeleton" :style="{ aspectRatio: imagePlaceholderRatio(imageRequestId(message)!) }" role="status" aria-label="正在载入图片"></div>
          </template>
          <template v-else-if="!awaitingCost(imageRequestId(message)!) && ['generating', 'ready', 'summarized'].includes(imagePhases?.[imageRequestId(message)!]?.status ?? '')">
            <div class="chat-image-wave" role="status" :aria-label="imagePhases?.[imageRequestId(message)!]?.status === 'generating' ? '正在生图' : '正在载入图片'">
              <span v-for="(character, position) in (imagePhases?.[imageRequestId(message)!]?.status === 'generating' ? '正在生图' : '正在载入图片')" :key="position" :style="{ animationDelay: `${position * 0.12}s` }" aria-hidden="true">{{ character }}</span>
            </div>
            <div class="chat-image-skeleton" :style="{ aspectRatio: imagePlaceholderRatio(imageRequestId(message)!) }" role="img" aria-label="图片生成中"></div>
          </template>
          <p v-else-if="imagePhases?.[imageRequestId(message)!]?.status === 'failed' && !hasFailureMessage(imageRequestId(message)!)" class="chat-inline-error" role="alert">{{ mediaJobs?.find((job) => job.generation_request_id === imageRequestId(message))?.error_message ?? '图片生成未完成，请查看错误记录。' }}</p>
        </div>
        <p v-if="homeMode && activityByMessage?.[message.id]" class="chat-inline-status chat-direct-status">{{ activityByMessage[message.id] }}</p>
        <ChatImageAttachment v-if="homeMode && messageMedia?.[message.id]?.type === 'image' && !isHomeImageArtifact(message)" :media="messageMedia[message.id]" @open="openImage" />
        <figure v-else-if="homeMode && messageMedia?.[message.id]?.type === 'video' && !isHomeImageArtifact(message)" class="chat-generated-image chat-message-media">
          <video :src="messageMedia[message.id].url" controls preload="metadata" />
          <figcaption>生成的视频</figcaption>
        </figure>
        <AssistantStreamingBlock v-if="homeMode && streamingByMessage?.[message.id]" :content="streamingByMessage[message.id]" :show-mark="false" />
        <p v-if="homeMode && errorMessageId === message.id && error" class="chat-inline-error" role="alert">{{ error }}</p>
        <section v-if="homeMode && inlineRuns?.[message.id]" class="chat-inline-activity" aria-live="polite">
          <p class="chat-inline-status">{{ homeStatus(inlineRuns[message.id].run, inlineRuns[message.id].approval) }}</p>
          <p v-if="fastCommerceSummary(inlineRuns[message.id].run)" class="chat-inline-qc">{{ fastCommerceSummary(inlineRuns[message.id].run) }}</p>
          <p v-if="qcSummary(inlineRuns[message.id].run) && !inlineRuns[message.id].approval" class="chat-inline-qc">{{ qcSummary(inlineRuns[message.id].run) }}</p>
          <p v-if="homeError(inlineRuns[message.id])" class="chat-inline-error" role="alert">{{ homeError(inlineRuns[message.id]) }}</p>
          <figure v-if="inlineRuns[message.id].imageUrl" class="chat-generated-image"><img :src="inlineRuns[message.id].imageUrl" alt="生成的图片" /><figcaption>{{ inlineRuns[message.id].artifact ? '图片已保存' : '图片已生成，等待审核' }}</figcaption></figure>
          <figure v-if="inlineRuns[message.id].videoUrl" class="chat-generated-image"><video :src="inlineRuns[message.id].videoUrl" controls preload="metadata" /><figcaption>视频已保存</figcaption></figure>
          <p v-else-if="inlineRuns[message.id].artifact?.type === 'video'" class="chat-inline-status">视频已保存，预览暂不可用。</p>
          <ApprovalCard v-if="inlineRuns[message.id].approval" :approval="inlineRuns[message.id].approval!" :domain="inlineRuns[message.id].run.domain" :busy="approvalBusy ?? false" :image-url="inlineRuns[message.id].imageUrl" home-mode @decide="(action, response) => $emit('decideInline', message.id, action, response)" />
        </section>
        <div v-if="!homeMode && activityAnchor === index + 1 && activityLines.length" class="runtime-activity-list anchored" aria-label="任务进度">
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
      <AssistantStreamingBlock v-if="!homeMode && streaming" :content="streaming" />
      <div v-if="!homeMode && activityAnchor === null && activityLines.length" class="runtime-activity-list" aria-label="任务进度">
        <div v-for="event in activityLines" :key="event.id" class="runtime-activity" :data-event="event.event_type">
          <span>{{ ['node_completed', 'run_completed', 'artifact_created'].includes(event.event_type) ? '✓' : event.event_type.includes('failed') ? '!' : '●' }}</span>
          <span>{{ activityText(event) }}</span>
        </div>
      </div>
      <figure v-if="!homeMode && imageUrl" class="chat-generated-image"><img :src="imageUrl" alt="已生成的图片" /><figcaption>{{ run?.status === 'completed' ? '生成结果 · 已完成' : '生成结果 · 等待人工审核后归档' }}</figcaption></figure>
      <WorkflowActivity v-if="!homeMode && run" :run="run" @open="$emit('openRun', $event)" />
      <ErrorRecoveryPanel v-if="!homeMode && error" :message="error" @retry="$emit('retry')" />
    </div>
    <button v-if="!atBottom" type="button" class="back-bottom" @click="bottom(true)"><ArrowDown :size="14" />回到底部</button>
    <dialog ref="previewDialog" class="chat-image-preview" aria-label="图片预览" @close="previewImage = null; resetPreview()" @click="($event.target === $event.currentTarget) && closeImage()">
      <div v-if="previewImage" class="chat-image-preview-inner">
        <header><span>图片预览 <small>{{ Math.round(previewScale * 100) }}%</small></span><div>
          <button type="button" aria-label="恢复适应窗口" @click="resetPreview"><RotateCcw :size="18" aria-hidden="true" /><span>适应窗口</span></button>
          <a :href="previewImage.url" :download="previewImage.filename" aria-label="下载原图"><Download :size="18" aria-hidden="true" /><span class="chat-image-tooltip" role="tooltip">下载原图</span></a>
          <button type="button" aria-label="关闭预览" @click="closeImage"><X :size="19" aria-hidden="true" /></button>
        </div></header>
        <div ref="previewStage" class="chat-image-preview-stage" @wheel.prevent="zoomPreview" @click.self="closeImage">
          <img ref="previewElement" :src="previewImage.url" alt="生成图片的放大预览" :class="{ 'can-drag': previewScale > 1, dragging }" :style="{ transform: `translate(${previewX}px, ${previewY}px) scale(${previewScale})` }" draggable="false" @pointerdown="beginDrag" @pointermove="moveDrag" @pointerup="endDrag" @pointercancel="endDrag" />
        </div>
      </div>
    </dialog>
  </div>
</template>
