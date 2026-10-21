<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { History, MessageSquareText, Plus, Workflow } from 'lucide-vue-next'
import WorkspaceShell from '../components/workspace/WorkspaceShell.vue'
import { conversationTaskTitle } from '../components/chat/conversationPresentation'
import ApprovalCard from '../components/approval/ApprovalCard.vue'
import ChatMessageList from '../components/chat/ChatMessageList.vue'
import ConversationHistory from '../components/chat/ConversationHistory.vue'
import MessageComposer from '../components/chat/MessageComposer.vue'
import { visibleFastDomain, type FastDomain } from '../components/chat/fastDomainSelection'
import SystemStatusInline from '../components/chat/SystemStatusInline.vue'
import { domains } from '../domains'
import { presenterFor } from '../domains/presenters'
import { navigate, route } from '../router'
import { createConversation, decideApproval, decideMediaCost, deleteConversation, getApprovals, getArtifact, getArtifactContentUrl, getArtifacts, getConversation, getConversations, getEvents, getRun, getTaskImageUrl, renameConversation, setConversationFastDomain, streamConversationMessage, subscribeRunEvents, type RuntimeEvent } from '../services/core'
import type { Conversation, ConversationMessage, CoreApproval, CoreArtifact, CoreRun, MediaJob } from '../types'

const props = withDefaults(defineProps<{ runs?: CoreRun[]; approvals?: CoreApproval[]; busy?: boolean; domain?: string; embedded?: boolean; initialRunId?: string; studioFocus?: boolean }>(), { runs: () => [], approvals: () => [], busy: false, embedded: false, studioFocus: false })
const emit = defineEmits<{ refresh: []; runCreated: [run: CoreRun]; chatting: [active: boolean]; selectStage: [nodeId: string] }>()
const conversations = ref<Conversation[]>([])
const historyOpen = ref(false)
const navOpen = ref(!window.matchMedia('(max-width: 600px)').matches)
/* 桌面端图标条折叠态：与 navOpen（抽屉显隐）独立，跨页共用同一 key 保持状态一致。
   localStorage 在 SSR / 单元测试环境下不存在，必须容错，否则 setup 阶段直接抛错。 */
const navCollapsedKey = 'kantoku-ws-nav-collapsed'
function readNavCollapsed(): boolean {
  try { return localStorage.getItem(navCollapsedKey) === '1' } catch { return false }
}
const navCollapsed = ref(readNavCollapsed())
function toggleNavCollapsed(value: boolean): void {
  navCollapsed.value = value
  try { localStorage.setItem(navCollapsedKey, value ? '1' : '0') } catch { /* 忽略写入失败 */ }
}
const creatingConversation = ref(false)
/* 首页形态：二级侧栏已整块移除（那里只有一个"对话"入口，等于一块空白竖条），
   「最近对话」改为顶栏按钮 + 浮层。与 studioFocus 的历史聊天按钮（historyOpen）
   是两套入口，各自独立，互不影响。 */
const recentOpen = ref(false)
const recentHost = ref<HTMLElement | null>(null)
function pickConversation(id: string): void {
  recentOpen.value = false
  void selectConversation(id)
}
function closeRecentOnOutside(event: MouseEvent): void {
  if (!recentOpen.value) return
  const target = event.target
  if (target instanceof Node && recentHost.value?.contains(target)) return
  recentOpen.value = false
}
/* 第二层侧栏 = 当前领域的工作节点；文案来自 Domain Presenter，首页不显示。 */
const navigation = computed(() => [
  { id: 'conversation', label: '对话', icon: MessageSquareText },
  ...(props.domain ? presenterFor(props.domain).workflow.map((node) => ({ id: node.id, label: node.label, icon: Workflow })) : []),
])
const activeWorkspacePage = computed(() => selectedWorkspaceNode.value || 'conversation')
const conversationTitle = computed(() => conversations.value.find(item => item.id === conversationId.value)?.title ?? '新对话')
const shellIdentity = computed(() => props.domain ? domainLabel(props.domain).name : 'Kantoku')
const shellSubtitle = computed(() => props.domain ? domainLabel(props.domain).label : '对话工作区')
function domainLabel(domain: string): { name: string; label: string } {
  const found = domains.find((item) => item.id === domain)
  return { name: found?.name ?? domain, label: found?.label ?? '' }
}
const conversationId = ref('')
const messages = ref<ConversationMessage[]>([])
const mediaJobs = ref<MediaJob[]>([])
const streaming = ref('')
const sending = ref(false)
const error = ref('')
const activeRun = ref<CoreRun | null>(null)
const domainHint = ref<string | null>(props.domain ?? null)
const fastDomain = ref<FastDomain | null>(null)
const fastDomainTaskId = ref<string | null>(null)
const switchingFastDomain = ref(false)
const dispatchingFastDomain = ref<Record<string, boolean>>({})
const consumedFastDomains = new Set<string>()
const pendingFastDispatches = new Map<string, Promise<void>>()
const dataMode = ref<'demo' | 'production'>('production')
const enhancePrompt = ref(true)
const composer = ref<{ fill: (text: string) => void } | null>(null)
const activities = ref<RuntimeEvent[]>([])
const localApprovals = ref<CoreApproval[]>([])
const lastContent = ref('')
const imageUrl = ref('')
interface InlineRunState { run: CoreRun; activities: RuntimeEvent[]; approval: CoreApproval | null; artifact: CoreArtifact | null; imageUrl: string; videoUrl: string }
interface QueuedMessage { id: string; conversationId: string; content: string; domainHint: string | null; title?: string; onBound?: () => void }
const inlineRuns = ref<Record<string, InlineRunState>>({})
const pendingMessages = ref<Record<string, 'queued' | 'replying' | 'failed'>>({})
const activityByMessage = ref<Record<string, string>>({})
const publicActivities = ref<Record<string, { kind: string; label: string; detail: string }[]>>({})
const messageMedia = ref<Record<string, { type: 'image' | 'video'; url: string; filename: string }>>({})
const messageMediaErrors = ref<Record<string, boolean>>({})
const imagePhases = ref<Record<string, { status: 'prepared' | 'generating' | 'ready' | 'summarized' | 'failed'; width: number; height: number }>>({})
const streamingByMessage = ref<Record<string, string>>({})
const errorMessageId = ref('')
const deciding = ref(false)
const homeStops = new Map<string, () => void>()
const runAnchors = new Map<string, string>()
const loadingMedia = new Set<string>()
const unavailableMedia = new Set<string>()
let localMessageCounter = 0
let stopEvents: (() => void) | null = null
let lastSequence = 0
let imageLoadVersion = 0
let mediaPollTimer: ReturnType<typeof setInterval> | null = null
let selectionVersion = 0
const pendingApproval = computed(() => [...props.approvals, ...localApprovals.value].find((item) => item.run_id === activeRun.value?.id && item.decision === 'pending') ?? null)

async function refreshConversations(): Promise<void> {
  const list = await getConversations(props.domain)
  conversations.value = list.filter((item) => props.domain
    ? item.execution_mode === 'professional' && item.domain === props.domain
    : item.execution_mode === 'fast')
}

function syncFastDomain(detail: Conversation): void {
  if (props.domain || detail.id !== conversationId.value) return
  if (!detail.domain) consumedFastDomains.delete(detail.id)
  fastDomain.value = visibleFastDomain(detail.domain, detail.fast_domain_task_id, consumedFastDomains.has(detail.id))
  fastDomainTaskId.value = detail.fast_domain_task_id
  domainHint.value = fastDomain.value
}

async function refreshFastDomain(ownerId: string): Promise<void> {
  const detail = await getConversation(ownerId).catch(() => null)
  if (detail) syncFastDomain(detail)
}

async function selectConversation(id: string): Promise<void> {
  const currentSelection = ++selectionVersion
  conversationId.value = id
  if (!props.domain) {
    localStorage.setItem('kantoku-home-active-conversation', id)
    navigate({ name: 'home', conversationId: id }, true)
  }
  messages.value = []
  activeRun.value = null
  error.value = ''; errorMessageId.value = ''; activities.value = []; streaming.value = ''
  fastDomain.value = null; domainHint.value = props.domain ?? null; fastDomainTaskId.value = null
  composer.value?.fill('')
  if (mediaPollTimer) { clearInterval(mediaPollTimer); mediaPollTimer = null }
  stopEvents?.(); stopEvents = null
  for (const stop of homeStops.values()) stop()
  for (const state of Object.values(inlineRuns.value)) {
    if (state.imageUrl) URL.revokeObjectURL(state.imageUrl)
    if (state.videoUrl) URL.revokeObjectURL(state.videoUrl)
  }
  for (const media of Object.values(messageMedia.value)) URL.revokeObjectURL(media.url)
  messageMedia.value = {}
  messageMediaErrors.value = {}
  imagePhases.value = {}
  mediaJobs.value = []
  activityByMessage.value = {}
  publicActivities.value = {}
  streamingByMessage.value = {}
  homeStops.clear(); runAnchors.clear(); inlineRuns.value = {}
  const detail = await getConversation(id)
  if (!detail || currentSelection !== selectionVersion) return
  conversationId.value = id
  syncFastDomain(detail)
  domainHint.value = props.domain ?? fastDomain.value
  messages.value = detail.messages ?? []
  restoreMediaJobs(detail.media_jobs ?? [])
  if (mediaJobs.value.some((job) => (job.status === 'pending' || job.status === 'generating') && job.approval_status !== 'pending')) {
    startMediaPolling(id)
  }
  for (const message of messages.value) if (message.artifact_id) void loadMessageMedia(message, id)
  streaming.value = ''
  error.value = ''
  errorMessageId.value = ''
  activities.value = []
  const restoredRun = detail.active_run_id ? await getRun(detail.active_run_id) : null
  if (currentSelection !== selectionVersion) return
  activeRun.value = restoredRun
  if (!props.domain) {
    const runIds = [...new Set(messages.value.map((message) => message.run_id).filter((runId): runId is string => !!runId))]
    for (const runId of runIds.slice(-10)) {
      if (runId === restoredRun?.id) continue
      const run = await getRun(runId)
      if (currentSelection !== selectionVersion) return
      const anchor = [...messages.value].reverse().find((message) => message.run_id === runId)?.id
      if (run && anchor) followHomeRun(run, anchor)
    }
  }
  if (activeRun.value) {
    if (props.domain) followRun(activeRun.value.id)
    else {
      const anchor = [...messages.value].reverse().find((item) => item.run_id === activeRun.value?.id)?.id
        ?? [...messages.value].reverse().find((item) => item.role === 'assistant')?.id
        ?? messages.value.at(-1)?.id
      if (anchor) followHomeRun(activeRun.value, anchor)
    }
  }
}

function startMediaPolling(ownerId: string): void {
  if (!mediaPollTimer) mediaPollTimer = setInterval(() => { void refreshActiveMedia(ownerId) }, 1200)
}

function restoreMediaJobs(jobs: MediaJob[]): void {
  mediaJobs.value = jobs
  const phases = { ...imagePhases.value }
  for (const job of jobs) {
    if (job.media_type !== 'image') continue
    phases[job.generation_request_id] = {
      status: job.status === 'completed' ? 'ready'
        : job.status === 'failed' ? 'failed'
          : job.status === 'generating' ? 'generating' : 'prepared',
      width: phases[job.generation_request_id]?.width ?? 1,
      height: phases[job.generation_request_id]?.height ?? 1,
    }
  }
  imagePhases.value = phases
}

async function refreshActiveMedia(ownerId: string): Promise<void> {
  try {
    const detail = await getConversation(ownerId)
    if (!detail || conversationId.value !== ownerId) return
    syncFastDomain(detail)
    if (error.value === '连接中断，正在恢复图片任务状态。') error.value = ''
    restoreMediaJobs(detail.media_jobs ?? [])
    const persisted = detail.messages ?? []
    const pendingLocal = messages.value.filter((message) =>
      message.id.startsWith('local-') && pendingMessages.value[message.id]
      && !persisted.some((item) => item.event_id === `generation-user:${message.id}`),
    )
    messages.value = [...persisted, ...pendingLocal]
    for (const message of persisted) {
      if (message.artifact_id && !messageMedia.value[message.id] && !messageMediaErrors.value[message.id]) {
        void loadMessageMedia(message, ownerId)
      }
    }
    if (!mediaJobs.value.some((job) => (job.status === 'pending' || job.status === 'generating') && job.approval_status !== 'pending') && mediaPollTimer) {
      clearInterval(mediaPollTimer)
      mediaPollTimer = null
    }
  } catch (pollError) {
    if (conversationId.value === ownerId && !error.value) error.value = pollError instanceof Error ? pollError.message : '无法更新图片状态'
  }
}

async function loadMessageMedia(message: ConversationMessage, ownerId: string): Promise<void> {
  if (!message.artifact_id) return
  const loadingKey = `conversation:${message.id}`
  if (loadingMedia.has(loadingKey) || messageMedia.value[message.id]) return
  loadingMedia.add(loadingKey)
  try {
    const [artifact, url] = await Promise.all([
      getArtifact(message.artifact_id).catch(() => null),
      getArtifactContentUrl(message.artifact_id).catch(() => null),
    ])
    if (!url) {
      if (conversationId.value === ownerId) messageMediaErrors.value = { ...messageMediaErrors.value, [message.id]: true }
      return
    }
    if (conversationId.value !== ownerId || artifact?.conversation_id !== ownerId ||
        !['image', 'video'].includes(artifact.type)) {
      URL.revokeObjectURL(url)
      return
    }
    messageMedia.value = {
      ...messageMedia.value,
      [message.id]: {
        type: artifact.type as 'image' | 'video', url,
        filename: `kantoku-${artifact.id}.${artifact.location?.match(/\.(png|jpe?g|webp)$/i)?.[1]?.toLowerCase() ?? 'png'}`,
      },
    }
  } finally {
    loadingMedia.delete(loadingKey)
  }
}

const selectedWorkspaceNode = ref('')
/** 点击第二层节点：交给领域 Workspace 打开对应阶段页面。 */
function selectWorkspaceNode(id: string): void {
  selectedWorkspaceNode.value = id === 'conversation' ? '' : id
  emit('selectStage', id)
}

async function newConversation(): Promise<void> {
  if (creatingConversation.value) return
  creatingConversation.value = true
  try {
    const created = await createConversation(props.domain ? 'guided' : 'autonomous', props.domain)
    await selectConversation(created.id)
    await refreshConversations()
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '新建对话失败' }
  finally { creatingConversation.value = false }
}

async function selectFastDomain(domain: FastDomain | null): Promise<void> {
  if (props.domain || switchingFastDomain.value || pendingFastDispatches.has(conversationId.value)) return
  if (!conversationId.value) await initialize()
  if (!conversationId.value || domain === fastDomain.value) return
  switchingFastDomain.value = true
  try {
    const updated = await setConversationFastDomain(conversationId.value, domain)
    consumedFastDomains.delete(conversationId.value)
    fastDomain.value = updated.domain as FastDomain | null
    domainHint.value = fastDomain.value
    error.value = ''
    await refreshConversations()
  } catch (taskError) {
    error.value = taskError instanceof Error ? taskError.message : '快捷模式切换失败'
  } finally { switchingFastDomain.value = false }
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
    const storedId = route.value.conversationId ?? localStorage.getItem('kantoku-home-active-conversation')
    const conversation = (!props.domain ? conversations.value.find(item => item.id === storedId) : null)
      ?? conversations.value.find((item) => !!props.initialRunId && item.active_run_id === props.initialRunId) ?? conversations.value[0]
    if (conversation) await selectConversation(conversation.id)
    else await newConversation()
    if (props.initialRunId && activeRun.value?.id !== props.initialRunId) {
      activeRun.value = await getRun(props.initialRunId)
      if (activeRun.value) {
        if (props.domain) followRun(activeRun.value.id)
        else {
          const anchor = messages.value.at(-1)?.id
          if (anchor) followHomeRun(activeRun.value, anchor)
        }
      }
    }
  } catch (taskError) {
    error.value = taskError instanceof Error ? taskError.message : '无法载入对话'
  }
}

function updateInline(runId: string, patch: Partial<InlineRunState>): void {
  const anchor = runAnchors.get(runId)
  if (!anchor || !inlineRuns.value[anchor]) return
  inlineRuns.value = { ...inlineRuns.value, [anchor]: { ...inlineRuns.value[anchor], ...patch } }
}

async function refreshInlineRun(runId: string, includeDetails = true): Promise<void> {
  const [run, approvals, artifacts] = await Promise.all([
    getRun(runId).catch(() => null),
    includeDetails ? getApprovals(`home-approvals:${runId}`).catch(() => null) : Promise.resolve(null),
    includeDetails ? getArtifacts(runId).catch(() => null) : Promise.resolve(null),
  ])
  if (run) {
    activeRun.value = run
    updateInline(runId, { run })
    if (['completed', 'failed', 'cancelled'].includes(run.status) && conversationId.value) {
      void refreshFastDomain(conversationId.value)
    }
  }
  const artifact = artifacts?.find((item) => item.type === 'image' || item.type === 'video') ?? null
  const patch: Partial<InlineRunState> = {}
  if (approvals) patch.approval = approvals.find((item) => item.run_id === runId && item.decision === 'pending') ?? null
  if (artifacts) patch.artifact = artifact
  updateInline(runId, patch)
  const mediaKey = `${runId}:${artifact?.id ?? ''}`
  const current = inlineRuns.value[runAnchors.get(runId) ?? '']
  if (artifact && !loadingMedia.has(mediaKey) && !unavailableMedia.has(mediaKey) && !(artifact.type === 'image' ? current?.imageUrl : current?.videoUrl)) {
    loadingMedia.add(mediaKey)
    const media = await getArtifactContentUrl(artifact.id).catch(() => null)
    if (media && inlineRuns.value[runAnchors.get(runId) ?? '']) {
      updateInline(runId, artifact.type === 'image' ? { imageUrl: media } : { videoUrl: media })
    } else if (media) URL.revokeObjectURL(media)
    else unavailableMedia.add(mediaKey)
    loadingMedia.delete(mediaKey)
  }
  const state = run?.state ?? inlineRuns.value[runAnchors.get(runId) ?? '']?.run.state
  const requestId = state?.request_id
  const currentImage = inlineRuns.value[runAnchors.get(runId) ?? '']?.imageUrl
  if (!currentImage && typeof requestId === 'string' && typeof state?.image_path === 'string' && state.image_path) {
    const url = await getTaskImageUrl(requestId).catch(() => null)
    const anchor = runAnchors.get(runId)
    if (url && anchor && inlineRuns.value[anchor]) {
      const old = inlineRuns.value[anchor].imageUrl
      updateInline(runId, { imageUrl: url })
      if (old) URL.revokeObjectURL(old)
    } else if (url) URL.revokeObjectURL(url)
  }
}

function followHomeRun(run: CoreRun, anchor: string): void {
  homeStops.get(run.id)?.()
  runAnchors.set(run.id, anchor)
  inlineRuns.value = {
    ...inlineRuns.value,
    [anchor]: inlineRuns.value[anchor] ?? { run, activities: [], approval: null, artifact: null, imageUrl: '', videoUrl: '' },
  }
  void getEvents(run.id).then((events) => {
    const current = inlineRuns.value[runAnchors.get(run.id) ?? '']
    if (!current) return
    const merged = new Map([...events, ...current.activities].map((event) => [event.sequence, event]))
    updateInline(run.id, { activities: [...merged.values()].sort((a, b) => a.sequence - b.sequence) })
  }).catch(() => {})
  void refreshInlineRun(run.id).catch(() => {})
  let lastSeen = 0
  const stop = subscribeRunEvents(run.id, 0, {
    onEvent: (event) => {
      if (event.sequence <= lastSeen) return
      lastSeen = event.sequence
      const message = event.payload.message as ConversationMessage | undefined
      if (message?.conversation_id === conversationId.value && !messages.value.some(item => item.id === message.id)) {
        messages.value.push(message)
      }
      const current = inlineRuns.value[runAnchors.get(run.id) ?? '']
      if (current) updateInline(run.id, { activities: [...current.activities.filter((item) => item.sequence !== event.sequence), event].sort((a, b) => a.sequence - b.sequence) })
      void refreshInlineRun(run.id, ['approval_required', 'approval_resolved', 'artifact_created', 'run_completed', 'run_failed', 'run_cancelled'].includes(event.event_type)).catch(() => {})
      emit('refresh')
    },
    onEnd: () => { homeStops.get(run.id)?.(); homeStops.delete(run.id); void refreshInlineRun(run.id).catch(() => {}); if (conversationId.value) void refreshFastDomain(conversationId.value) },
  })
  homeStops.set(run.id, stop)
}

async function runHomeMessage(task: QueuedMessage): Promise<void> {
  pendingMessages.value = { ...pendingMessages.value, [task.id]: 'replying' }
  let anchor = task.id
  let persistedUserMessageId: string | null = null
  let bound = false
  try {
    if (task.title) await renameConversation(task.conversationId, task.title)
    await streamConversationMessage(task.conversationId, task.content, task.domainHint, {
        onIntent: (plan) => {
          persistedUserMessageId = plan.user_message_id ?? null
          bound = true
          task.onBound?.()
          void refreshFastDomain(task.conversationId)
          if (plan.tool === 'workspace.open' && plan.domain === 'comic' && conversationId.value === task.conversationId) {
            navigate({ name: 'workspace', domain: 'comic', workspacePage: 'director',
              directorStage: 'creative_understanding', conversationId: task.conversationId, creationMode: 'professional' })
          }
        },
        onDelta: (delta) => {
          if (conversationId.value === task.conversationId) {
            streamingByMessage.value = {
              ...streamingByMessage.value,
              [task.id]: (streamingByMessage.value[task.id] ?? '') + delta,
            }
          }
        },
        onMessage: (message) => {
          if (conversationId.value !== task.conversationId) return
          const index = messages.value.findIndex((item) => item.id === anchor)
          if (!messages.value.some((item) => item.id === message.id)) {
            messages.value.splice(index < 0 ? messages.value.length : index + 1, 0, message)
          }
          if (message.artifact_id) void loadMessageMedia(message, task.conversationId)
          const nextActivity = { ...activityByMessage.value }
          if (message.role === 'assistant') delete nextActivity[task.id]
          activityByMessage.value = nextActivity
          for (const [runId, id] of runAnchors) {
            if (id !== anchor) continue
            runAnchors.set(runId, message.id)
            inlineRuns.value = { ...inlineRuns.value, [message.id]: inlineRuns.value[anchor] }
            const copy = { ...inlineRuns.value }; delete copy[anchor]; inlineRuns.value = copy
          }
          anchor = message.id
          const nextStreaming = { ...streamingByMessage.value }
          delete nextStreaming[task.id]
          streamingByMessage.value = nextStreaming
          void refreshConversations()
        },
        onRun: (run) => {
          if (conversationId.value === task.conversationId) {
            activeRun.value = run
            followHomeRun(run, anchor)
            emit('refresh'); emit('runCreated', run)
          }
        },
        onActivity: (activity) => {
          if (conversationId.value === task.conversationId && activity.generation_request_id === task.id) {
            activityByMessage.value = { ...activityByMessage.value, [task.id]: activity.label }
          }
        },
        onPublicActivity: (activity) => {
          if (conversationId.value !== task.conversationId) return
          const previous = publicActivities.value[task.id] ?? []
          const next = [...previous, activity]
          publicActivities.value = {
            ...publicActivities.value,
            [task.id]: next,
            ...(persistedUserMessageId ? { [persistedUserMessageId]: next } : {}),
          }
        },
        onImageEvent: (name, event) => {
          if (conversationId.value !== task.conversationId) return
          if (name === 'cost_approval') {
            void refreshActiveMedia(task.conversationId)
            return
          }
          if (name === 'image_generating') startMediaPolling(task.conversationId)
          if (event.generation_request_id !== task.id) return
          if (name === 'prompt_prepared' || name === 'image_generating') {
            imagePhases.value = { ...imagePhases.value, [task.id]: {
              status: name === 'prompt_prepared' ? 'prepared' : 'generating',
              width: event.width ?? imagePhases.value[task.id]?.width ?? 1,
              height: event.height ?? imagePhases.value[task.id]?.height ?? 1,
            } }
          } else if (name === 'image_ready' || name === 'image_summary' || name === 'image_failed') {
            const previous = imagePhases.value[task.id] ?? { width: 1, height: 1 }
            imagePhases.value = { ...imagePhases.value, [task.id]: {
              ...previous,
              status: name === 'image_failed' ? 'failed' : name === 'image_summary' ? 'summarized' : 'ready',
            } }
          }
        },
      }, task.domainHint === 'commerce' ? 'demo' : dataMode.value, enhancePrompt.value, task.id, {
        selected_domain: task.domainHint, execution_mode: task.domainHint ? 'fast' : 'normal',
      })
    const next = { ...pendingMessages.value }; delete next[task.id]; pendingMessages.value = next
  } catch (taskError) {
    const imageStarted = imagePhases.value[task.id]?.status === 'prepared' || imagePhases.value[task.id]?.status === 'generating'
    if (conversationId.value === task.conversationId) {
      error.value = imageStarted ? '连接中断，正在恢复图片任务状态。' : taskError instanceof Error ? taskError.message : '消息没有发送成功'
      errorMessageId.value = task.id
    }
    pendingMessages.value = { ...pendingMessages.value, [task.id]: imageStarted ? 'replying' : 'failed' }
    if (imageStarted) startMediaPolling(task.conversationId)
    const nextActivity = { ...activityByMessage.value }; delete nextActivity[task.id]; activityByMessage.value = nextActivity
  } finally {
    if (!bound && task.domainHint) {
      const detail = await getConversation(task.conversationId).catch(() => null)
      if (detail?.domain === task.domainHint && !detail.fast_domain_task_id) {
        await setConversationFastDomain(task.conversationId, null).catch(() => null)
      }
    }
    task.onBound?.()
    const nextStreaming = { ...streamingByMessage.value }
    delete nextStreaming[task.id]
    streamingByMessage.value = nextStreaming
    if (conversationId.value === task.conversationId) {
      void refreshConversations()
      void refreshFastDomain(task.conversationId)
      void refreshActiveMedia(task.conversationId)
    }
  }
}

async function send(content: string): Promise<void> {
  if (!props.domain) {
    if (!conversationId.value) await initialize()
    if (!conversationId.value) return
    const ownerId = conversationId.value
    const current = conversations.value.find(item => item.id === ownerId)
    let taskTitle: string | undefined
    if (current?.title === '新对话') {
      taskTitle = conversationTaskTitle(content)
      current.title = taskTitle
    }
    const selectedDomain = fastDomain.value
    const earlierDispatch = pendingFastDispatches.get(ownerId)
    const id = `local-${Date.now()}-${++localMessageCounter}`
    messages.value.push({ id, conversation_id: ownerId, role: 'user', type: 'text', content, run_id: null, event_id: null, created_at: new Date().toISOString() })
    pendingMessages.value = { ...pendingMessages.value, [id]: earlierDispatch ? 'queued' : 'replying' }
    let onBound: (() => void) | undefined
    if (selectedDomain) {
      consumedFastDomains.add(ownerId)
      fastDomain.value = null
      domainHint.value = null
      let release!: () => void
      const gate = new Promise<void>((resolve) => { release = resolve })
      pendingFastDispatches.set(ownerId, gate)
      dispatchingFastDomain.value = { ...dispatchingFastDomain.value, [ownerId]: true }
      onBound = () => {
        if (pendingFastDispatches.get(ownerId) !== gate) return
        pendingFastDispatches.delete(ownerId)
        const next = { ...dispatchingFastDomain.value }; delete next[ownerId]; dispatchingFastDomain.value = next
        release()
      }
    }
    if (earlierDispatch) {
      try {
        await earlierDispatch
        const detail = await getConversation(ownerId)
        if (!detail) throw new Error('无法确认上一条快捷任务状态，请稍后重试')
        if (detail.domain && !detail.fast_domain_task_id && consumedFastDomains.has(ownerId)) {
          await setConversationFastDomain(ownerId, null)
        }
      } catch (taskError) {
        if (conversationId.value === ownerId) {
          error.value = taskError instanceof Error ? taskError.message : '无法确认上一条快捷任务状态'
          errorMessageId.value = id
        }
        pendingMessages.value = { ...pendingMessages.value, [id]: 'failed' }
        return
      }
    }
    void runHomeMessage({ id, content, conversationId: ownerId, domainHint: selectedDomain, title: taskTitle, onBound })
    return
  }
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
    }, dataMode.value, enhancePrompt.value)
  } catch (taskError) {
    error.value = taskError instanceof Error ? taskError.message : '消息没有发送成功'
  } finally { sending.value = false }
}

function followRun(runId: string): void {
  stopEvents?.()
  lastSequence = 0
  void getEvents(runId).then((events) => { activities.value = events })
  void getApprovals().then((items) => { localApprovals.value = items ?? [] })
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
    localApprovals.value = await getApprovals() ?? []
    if (activeRun.value) followRun(activeRun.value.id)
    emit('refresh')
  } catch (taskError) {
    error.value = taskError instanceof Error ? taskError.message : '审批没有提交成功'
  } finally { sending.value = false }
}

async function decideInline(messageId: string, action: 'approve' | 'reject' | 'revise', response: Record<string, unknown>): Promise<void> {
  const approval = inlineRuns.value[messageId]?.approval
  if (!approval || deciding.value) return
  deciding.value = true
  try {
    const run = await decideApproval(approval.id, action, response)
    updateInline(run.id, { run, approval: null })
    followHomeRun(run, messageId)
    emit('refresh')
  } catch (taskError) {
    error.value = taskError instanceof Error ? taskError.message : '确认没有提交成功'
    errorMessageId.value = messageId
  } finally { deciding.value = false }
}

async function decideMedia(requestId: string, decision: 'approve' | 'reject'): Promise<void> {
  if (!conversationId.value || deciding.value) return
  deciding.value = true
  try {
    await decideMediaCost(conversationId.value, requestId, decision)
    await refreshActiveMedia(conversationId.value)
    if (decision === 'approve') startMediaPolling(conversationId.value)
    else await refreshFastDomain(conversationId.value)
  } catch (taskError) {
    error.value = taskError instanceof Error ? taskError.message : '费用确认失败'
  } finally { deciding.value = false }
}

function openRun(run: CoreRun): void { navigate({ name: 'workspace_run', domain: run.domain, runId: run.id }) }
onMounted(() => {
  // 点击浮层外部关闭；SSR/单测环境可能没有 document，这只是增强，缺了不影响功能。
  try { document.addEventListener('click', closeRecentOnOutside) } catch { /* 忽略 */ }
  void initialize()
})
onBeforeUnmount(() => {
  try { document.removeEventListener('click', closeRecentOnOutside) } catch { /* 忽略 */ }
  if (mediaPollTimer) clearInterval(mediaPollTimer)
  stopEvents?.()
  for (const stop of homeStops.values()) stop()
  for (const state of Object.values(inlineRuns.value)) {
    if (state.imageUrl) URL.revokeObjectURL(state.imageUrl)
    if (state.videoUrl) URL.revokeObjectURL(state.videoUrl)
  }
  for (const media of Object.values(messageMedia.value)) URL.revokeObjectURL(media.url)
  if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
})
watch(() => props.domain, (value) => { domainHint.value = value ?? null })
watch(
  () => [activeRun.value?.id, activeRun.value?.state?.request_id, activeRun.value?.state?.image_path] as const,
  async ([, requestId, imagePath]) => {
    const version = ++imageLoadVersion
    if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
    imageUrl.value = ''
    if (typeof requestId !== 'string' || typeof imagePath !== 'string' || !imagePath) return
    const loaded = await getTaskImageUrl(requestId).catch(() => null)
    if (version !== imageLoadVersion) { if (loaded) URL.revokeObjectURL(loaded); return }
    imageUrl.value = loaded ?? ''
  },
)
watch(
  () => [messages.value.length, activeRun.value?.id] as const,
  ([count, runId]) => emit('chatting', count > 0 && !runId),
)
</script>

<template>
  <WorkspaceShell
    class="home-workspace"
    :navigation="navigation"
    :active-page="activeWorkspacePage"
    v-model:navigation-open="navOpen"
    :navigation-collapsed="navCollapsed"
    :hide-navigation="!domain"
    :flow-composer="!domain"
    :creating-conversation="creatingConversation"
    :identity="shellIdentity"
    :subtitle="shellSubtitle"
    :workspace-label="domain ? `${shellIdentity} 工作台` : '首页对话工作台'"
    new-conversation-label="新建对话"
    hide-heading
    @update:navigation-collapsed="toggleNavCollapsed"
    @new-conversation="newConversation"
    @select-page="selectWorkspaceNode"
  >
    <template #toolbar>
      <strong class="home-conversation-title">{{ conversationTitle }}</strong>
      <slot name="workspace-toolbar" />
      <!-- 首页：二级侧栏已移除，新建对话与最近对话改由顶栏承载。 -->
      <div v-if="!domain" ref="recentHost" class="home-toolbar-actions">
        <button class="ui-button quiet sm" type="button" :disabled="creatingConversation" @click="newConversation"><Plus :size="15" />新建对话</button>
        <div class="home-recent">
          <button class="ui-button quiet sm" type="button" :aria-expanded="recentOpen" aria-label="最近对话" @click="recentOpen = !recentOpen"><History :size="15" />最近对话</button>
          <Transition name="home-pop">
            <div v-if="recentOpen" class="home-recent-pop" role="dialog" aria-label="最近对话列表">
              <ConversationHistory list-only :conversations="conversations" :active-id="conversationId" :busy="creatingConversation" @select="pickConversation" @rename="renameChat" @remove="removeChat" />
            </div>
          </Transition>
        </div>
      </div>
    </template>
    <template #stage><slot name="workspace-stage" /></template>
    <template #recent><ConversationHistory list-only :conversations="conversations" :active-id="conversationId" :busy="creatingConversation" @select="selectConversation" @rename="renameChat" @remove="removeChat" /></template>
    <template #composer>
      <div v-if="domain" class="composer-options"><label class="enhance-toggle"><input v-model="enhancePrompt" type="checkbox" />AI 优化提示词</label><small>先优化提示词，再进入专业制作流程</small></div>
      <MessageComposer v-if="!domain" :key="conversationId" ref="composer" fast-domains :fast-domain="fastDomain" :fast-domain-busy="switchingFastDomain || !!dispatchingFastDomain[conversationId]" @select-fast-domain="selectFastDomain" @send="send" />
      <MessageComposer v-else :key="conversationId" ref="composer" :disabled="sending" @send="send" />
      <p class="home-composer-hint">{{ domain ? '描述目标，监督酱会逐步确认细节与结果。' : fastDomain === 'comic' ? '自动完成分析与生图，图片会保留在当前对话。' : fastDomain ? '快捷模式会自动处理；只有费用或必要审核才会请你决定。' : '直接提问或描述创意，结果会保留在当前对话。' }}</p>
    </template>
    <div class="chat-home" :class="{ embedded, 'fast-chat': !domain }">
    <header v-if="domain" class="chat-home-head">
      <div v-if="studioFocus"><h1>AI 导演</h1></div>
      <div v-else><span class="section-kicker">{{ domain ? `${domain.toUpperCase()} WORKSPACE` : 'AUTONOMOUS CONVERSATION' }}</span><h1>{{ domain ? '与 Kantoku 协作' : '和 Kantoku 一起工作' }}</h1><p>{{ domain ? '描述目标，逐步确认制作细节与结果。' : '直接提问或描述你想制作的内容，结果会留在当前聊天。' }}</p></div>
      <button v-if="studioFocus" type="button" class="studio-history-toggle" :aria-expanded="historyOpen" aria-label="查看历史聊天" @click="historyOpen = !historyOpen"><History :size="15" /> 历史聊天</button>
      <SystemStatusInline :label="sending || Object.keys(streamingByMessage).length ? 'Kantoku 正在回复' : '就绪'" :tone="sending || Object.keys(streamingByMessage).length ? 'active' : 'neutral'" />
    </header>
    <div v-if="domain === 'commerce'" class="commerce-mode"><span>商品数据</span><button type="button" :aria-pressed="dataMode === 'production'" @click="dataMode = 'production'">Production</button><button type="button" :aria-pressed="dataMode === 'demo'" @click="dataMode = 'demo'">DEMO · Mock Data</button><strong v-if="dataMode === 'demo'">模拟数据，不代表真实市场商品</strong></div>
    <ChatMessageList :messages="messages" :media-jobs="mediaJobs" :streaming="streaming" :streaming-by-message="streamingByMessage" :run="activeRun" :activities="activities" :public-activities="publicActivities" :image-url="imageUrl" :inline-runs="inlineRuns" :pending-messages="pendingMessages" :activity-by-message="activityByMessage" :image-phases="imagePhases" :message-media="messageMedia" :message-media-errors="messageMediaErrors" :error-message-id="errorMessageId" :home-mode="!domain" :error="error" :empty-hint="domain ? presenterFor(domain).guideHint : undefined" :examples="domain ? presenterFor(domain).examples : undefined" :approval-busy="deciding" @retry="lastContent && send(lastContent)" @open-run="openRun" @decide-inline="decideInline" @decide-media="decideMedia" @example="(text) => composer?.fill(text)" />
    <div v-if="domain && pendingApproval" class="home-approval"><ApprovalCard :approval="pendingApproval" :domain="activeRun?.domain ?? 'studio'" :busy="sending" :image-url="imageUrl" @decide="decide" /></div>
    </div>
  </WorkspaceShell>
</template>

<style scoped>
.home-workspace .chat-home { height:100%; min-height:0; width:100%; }
.home-conversation-title { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:14px; font-weight:600; }
.home-composer-hint { margin:8px 0 0; color:var(--text-muted); font-size:11px; text-align:center; }
/* 首页流式布局：对话区高度交给内容决定，滚动由 WorkspaceShell 的 .ws-shell-body.is-flow 承担。
   这里必须用 :deep() —— .ws-shell-body 属于 WorkspaceShell，本组件的 scoped 属性不落在它身上；
   同时该选择器要比上面那条 .home-workspace .chat-home 更具体，才能真正覆盖 height。 */
.home-workspace :deep(.ws-shell-body.is-flow) .chat-home { height:auto; }
.home-toolbar-actions { display:flex; align-items:center; gap:8px; margin-left:auto; }
.home-recent { position:relative; }
.home-recent-pop { position:absolute; z-index:30; top:calc(100% + 8px); right:0; display:flex; flex-direction:column; width:min(300px, 80vw); max-height:min(440px, 62vh); padding:12px 12px 4px; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-panel); box-shadow:var(--shadow-pop); }
.home-recent-pop :deep(.conversation-list) { min-height:0; max-height:340px; }
/* 浮层进出：与全站一致的克制动效（位移 4px，无回弹）。 */
.home-pop-enter-active, .home-pop-leave-active { transition:opacity var(--duration-fast) var(--ease-standard), transform var(--duration-fast) var(--ease-out); }
.home-pop-enter-from, .home-pop-leave-to { opacity:0; transform:translateY(-4px); }
@media (prefers-reduced-motion:reduce) { .home-pop-enter-active, .home-pop-leave-active { transition:none; } }
</style>
