<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ApprovalCard from '../components/approval/ApprovalCard.vue'
import ChatMessageList from '../components/chat/ChatMessageList.vue'
import ConversationHistory from '../components/chat/ConversationHistory.vue'
import MessageComposer from '../components/chat/MessageComposer.vue'
import SystemStatusInline from '../components/chat/SystemStatusInline.vue'
import { presenterFor } from '../domains/presenters'
import { navigate } from '../router'
import { createConversation, decideApproval, deleteConversation, getApprovals, getConversation, getConversations, getEvents, getRun, renameConversation, streamConversationMessage, subscribeRunEvents, type RuntimeEvent } from '../services/core'
import type { Conversation, ConversationMessage, CoreApproval, CoreRun } from '../types'

const props = withDefaults(defineProps<{ runs?: CoreRun[]; approvals?: CoreApproval[]; busy?: boolean; domain?: string; embedded?: boolean; initialRunId?: string }>(), { runs: () => [], approvals: () => [], busy: false, embedded: false })
const emit = defineEmits<{ refresh: []; runCreated: [run: CoreRun]; chatting: [active: boolean] }>()
const conversations = ref<Conversation[]>([])
const conversationId = ref('')
const messages = ref<ConversationMessage[]>([])
const streaming = ref('')
const sending = ref(false)
const error = ref('')
const activeRun = ref<CoreRun | null>(null)
const domainHint = ref<string | null>(props.domain ?? null)
const dataMode = ref<'demo' | 'production'>('production')
const enhancePrompt = ref(true)
const composer = ref<{ fill: (text: string) => void } | null>(null)
const activities = ref<RuntimeEvent[]>([])
const localApprovals = ref<CoreApproval[]>([])
const lastContent = ref('')
let stopEvents: (() => void) | null = null
let lastSequence = 0
const pendingApproval = computed(() => [...props.approvals, ...localApprovals.value].find((item) => item.run_id === activeRun.value?.id && item.decision === 'pending') ?? null)

async function refreshConversations(): Promise<void> {
  const list = await getConversations(props.domain)
  conversations.value = props.domain ? list : list.filter((item) => !item.domain)
}

async function selectConversation(id: string): Promise<void> {
  stopEvents?.(); stopEvents = null
  const detail = await getConversation(id)
  if (!detail) return
  conversationId.value = id
  messages.value = detail.messages ?? []
  streaming.value = ''
  error.value = ''
  activities.value = []
  activeRun.value = detail.active_run_id ? await getRun(detail.active_run_id) : null
  if (activeRun.value) followRun(activeRun.value.id)
}

async function newConversation(): Promise<void> {
  const created = await createConversation(props.domain ? 'guided' : 'autonomous', props.domain)
  await refreshConversations()
  await selectConversation(created.id)
}

async function renameChat(id: string, title: string): Promise<void> {
  await renameConversation(id, title)
  await refreshConversations()
}

async function removeChat(id: string): Promise<void> {
  if (!window.confirm('只删除这条聊天记录？关联的任务、产物和费用记录会保留。')) return
  await deleteConversation(id)
  await refreshConversations()
  if (id === conversationId.value) {
    if (conversations.value[0]) await selectConversation(conversations.value[0].id)
    else await newConversation()
  }
}

async function initialize(): Promise<void> {
  try {
    await refreshConversations()
    const conversation = conversations.value.find((item) => item.active_run_id === props.initialRunId) ?? conversations.value[0]
    if (conversation) await selectConversation(conversation.id)
    else await newConversation()
    if (props.initialRunId && activeRun.value?.id !== props.initialRunId) {
      activeRun.value = await getRun(props.initialRunId)
      if (activeRun.value) followRun(activeRun.value.id)
    }
  } catch (taskError) {
    error.value = taskError instanceof Error ? taskError.message : '无法载入对话'
  }
}

async function send(content: string): Promise<void> {
  if (sending.value) return
  if (!conversationId.value) await initialize()
  lastContent.value = content
  error.value = ''
  sending.value = true
  messages.value.push({ id: `local-${Date.now()}`, conversation_id: conversationId.value, role: 'user', type: 'text', content, run_id: null, event_id: null, created_at: new Date().toISOString() })
  streaming.value = ''
  try {
    await streamConversationMessage(conversationId.value, content, domainHint.value, {
      onDelta: (delta) => { streaming.value += delta },
      onMessage: (message) => { messages.value.push(message); streaming.value = ''; void refreshConversations() },
      onRun: (run) => { activeRun.value = run; followRun(run.id); emit('refresh'); emit('runCreated', run) },
    }, dataMode.value, props.domain ? enhancePrompt.value : false)
  } catch (taskError) {
    error.value = taskError instanceof Error ? taskError.message : '消息没有发送成功'
  } finally { sending.value = false }
}

function followRun(runId: string): void {
  stopEvents?.()
  lastSequence = 0
  void getEvents(runId).then((events) => { activities.value = events })
  stopEvents = subscribeRunEvents(runId, 0, {
    onEvent: (event) => {
      if (event.sequence <= lastSequence) return
      lastSequence = event.sequence
      activities.value = [...activities.value.filter((item) => item.sequence !== event.sequence), event].sort((a, b) => a.sequence - b.sequence)
      void getRun(runId).then((run) => { if (run) activeRun.value = run })
      if (event.event_type === 'approval_required' || event.event_type === 'approval_resolved') void getApprovals().then((items) => { localApprovals.value = items ?? [] })
      emit('refresh')
    },
    onEnd: () => { stopEvents?.(); stopEvents = null; emit('refresh') },
  })
}

async function decide(action: 'approve' | 'reject' | 'revise', response: Record<string, unknown>): Promise<void> {
  if (!pendingApproval.value) return
  sending.value = true
  try {
    activeRun.value = await decideApproval(pendingApproval.value.id, action, response)
    emit('refresh')
  } catch (taskError) {
    error.value = taskError instanceof Error ? taskError.message : '审批没有提交成功'
  } finally { sending.value = false }
}

function openRun(run: CoreRun): void { navigate({ name: 'workspace_run', domain: run.domain, runId: run.id }) }
onMounted(initialize)
onBeforeUnmount(() => stopEvents?.())
watch(() => props.domain, (value) => { domainHint.value = value ?? null })
watch(
  () => [messages.value.length, activeRun.value?.id] as const,
  ([count, runId]) => emit('chatting', count > 0 && !runId),
)
</script>

<template>
  <main class="chat-shell" :class="{ embedded }">
    <ConversationHistory :conversations="conversations" :active-id="conversationId" :busy="sending" @create="newConversation" @select="selectConversation" @rename="renameChat" @remove="removeChat" />
    <div class="chat-home" :class="{ embedded }">
    <header class="chat-home-head">
      <div><span class="section-kicker">{{ domain ? `${domain.toUpperCase()} WORKSPACE` : 'AUTONOMOUS CONVERSATION' }}</span><h1>{{ domain ? '与 Kantoku 协作' : '和 Kantoku 一起工作' }}</h1><p>直接提问或描述制作任务。需要执行时先给计划，再显示真实进度。</p></div>
      <SystemStatusInline :label="sending ? 'DeepSeek 正在响应' : 'Core 已连接'" :tone="sending ? 'active' : 'neutral'" />
    </header>
    <div v-if="!domain" class="domain-hints" aria-label="可选领域提示">
      <span>可选提示</span>
      <button v-for="item in [{ id: 'commerce', label: '电商' }, { id: 'comic', label: '漫剧' }, { id: 'studio', label: '视觉创作' }]" :key="item.id" type="button" :aria-pressed="domainHint === item.id" @click="domainHint = domainHint === item.id ? null : item.id">{{ item.label }}</button>
    </div>
    <div v-if="domain === 'commerce'" class="commerce-mode"><span>商品数据</span><button type="button" :aria-pressed="dataMode === 'production'" @click="dataMode = 'production'">Production</button><button type="button" :aria-pressed="dataMode === 'demo'" @click="dataMode = 'demo'">DEMO · Mock Data</button><strong v-if="dataMode === 'demo'">模拟数据，不代表真实市场商品</strong></div>
    <ChatMessageList :messages="messages" :streaming="streaming" :run="activeRun" :activities="activities" :error="error" :empty-hint="domain ? presenterFor(domain).guideHint : undefined" :examples="domain ? presenterFor(domain).examples : undefined" @retry="lastContent && send(lastContent)" @open-run="openRun" @example="(text) => composer?.fill(text)" />
    <div v-if="pendingApproval" class="home-approval"><ApprovalCard :approval="pendingApproval" :domain="activeRun?.domain ?? 'studio'" :busy="sending" @decide="decide" /></div>
    <div class="composer-dock"><MessageComposer ref="composer" :disabled="sending" @send="send" /><p><label v-if="domain" class="enhance-toggle"><input v-model="enhancePrompt" type="checkbox" />AI 优化提示词</label>{{ domain ? '勾选后先把需求改写为详细生图提示词，再进入执行。' : '领域选择不是必填。Kantoku 会识别普通对话与生产任务。' }}</p></div>
    </div>
  </main>
</template>
