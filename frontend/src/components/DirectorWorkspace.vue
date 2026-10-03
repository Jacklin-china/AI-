<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Clapperboard, Layers, History, FileText, Film, MessageSquareText, PanelRight, ArrowLeft, ArrowRight, X, Check, Pencil, Trash2 } from 'lucide-vue-next'
import MessageComposer from './chat/MessageComposer.vue'
import UserMessageBubble from './chat/UserMessageBubble.vue'
import AssistantMessageBlock from './chat/AssistantMessageBlock.vue'
import ComicWorkspaceShell from './layout/ComicWorkspaceShell.vue'
import DirectorNodeView from './DirectorNodeView.vue'
import ChatImageAttachment from './chat/ChatImageAttachment.vue'
import { navigate, route } from '../router'
import { canConfirmDirector, canDispatchDirectorInput, chronologicalDirectorExecutions, directorConversationSummary, directorDraftFields, discardDirectorNodeDraft, editableDirectorDraft, directorFieldLabels, directorIsStale, directorNodeLabels, directorStageSections, fastDirectorNodeLabels, directorStateLabels, directorSummary, editableDirectorNode, selectDirectorExecution, stageDraftKey, workspaceProjectTitle } from '../domains/comic/directorPresentation'
import { CoreApiError, cancelRun, compileComicPrompt, confirmDirectorVersion, saveDirectorDraft, createComicProject, createConversation, createDirectorExecution, deleteConversation, getArtifactContentUrl, getComicAssets, getComicProject, getComicPromptVersions, getComicShots, getComicStoryboards, getConversation, getConversations, getCurrentDirector, getDirectorExecutions, getDirectorVersions, getEvents, getRun, renameConversation, restoreDirectorVersion, type ComicAssetView, type ComicProjectContext, type ComicShotView, type ComicStoryboardView, type CreationMode, type DirectorExecution, type RuntimeEvent } from '../services/core'
import type { Conversation, ConversationMessage } from '../types'
import type { CoreRun } from '../types'

const props = defineProps<{ initialRunId?: string }>()
const emit = defineEmits<{ newProject: [] }>()
const legacyOnly = ref(false)
const project = ref<ComicProjectContext | null>(null)
const mode = ref<CreationMode>('fast')
const executions = ref<DirectorExecution[]>([])
const runs = ref<Record<string, CoreRun>>({})
const assets = ref<ComicAssetView[]>([])
const boards = ref<ComicStoryboardView[]>([])
const shots = ref<ComicShotView[]>([])
const versions = ref<Record<string, unknown>[]>([])
const restoreChoice = ref<number | null>(null)
const prompts = ref<Record<string, unknown>[]>([])
const selectedBoard = ref('')
const selectedShot = ref('')
const selectedRun = ref('')
const selectedStage = ref(route.value.directorStage ?? 'director_assemble')
const section = ref(route.value.workspacePage === 'conversation' ? 'director' : route.value.workspacePage ?? 'director')
const navOpen = ref(!window.matchMedia('(max-width: 600px)').matches)
const inspectorOpen = ref(false)
const chatExpanded = ref(!route.value.workspacePage || route.value.workspacePage === 'conversation')
const composer = ref<InstanceType<typeof MessageComposer> | null>(null)
const timeline = ref<HTMLElement | null>(null)
const nearBottom = ref(true)
const busy = ref(false)
const cancelling = ref(false)
const queuedInputs = ref<{ id: number; text: string; mode: CreationMode }[]>([])
let nextInputId = 0
const loading = ref(true)
const error = ref('')
const pendingText = ref('')
const previousRunIds = ref<string[]>([])
const drafts = ref<Record<string, Record<string, string>>>({})
const revisionVersion = ref<number | null>(null)
const restoredSpec = ref<Record<string, unknown> | null>(null)
const events = ref<RuntimeEvent[]>([])
const conversationId = ref('')
const compiling = ref(false)
const stageScroll = ref<HTMLElement | null>(null)
const scrollPositions = new Map<string, number>()
const referenceUrls = ref<Record<string, string>>({})
const referenceErrors = ref<Record<string, string>>({})
const referenceLoading = new Set<string>()
let timer: ReturnType<typeof setInterval> | null = null
let disposed = false
let refreshing = false
let applyingRoute = false
let pageRequest = 0
const navigation = [
  { id: 'conversation', label: '对话', icon: MessageSquareText },
  { id: 'director', label: '导演', icon: Clapperboard },
  { id: 'storyboard', label: '分镜', icon: Film },
  { id: 'assets', label: '资产', icon: Layers },
  { id: 'prompt', label: 'Prompt', icon: FileText },
  { id: 'history', label: '历史', icon: History },
]
/* 对话线程与工作流执行记录是两类数据：左侧只列用户 ↔ AI 的聊天线程。 */
const conversations = ref<Conversation[]>([])
const conversationSearch = ref('')
const editingConversation = ref('')
const conversationTitle = ref('')
const conversationBusy = ref(false)
const activeConversationId = ref('')
/* Conversation（用户 ↔ AI 聊天）与 Run History（执行记录）是两套数据，这里只装聊天。 */
const conversationMessages = ref<ConversationMessage[]>([])
interface ChatTurn { id: string; role: 'user' | 'assistant'; content: string }
async function loadConversationMessages(id: string): Promise<void> {
  if (!id) { conversationMessages.value = []; return }
  const loaded = await getConversation(id).catch(() => null)
  conversationMessages.value = (loaded?.messages ?? []).filter(item => item.role !== 'system' && item.content.trim())
}
const chatTurns = computed<ChatTurn[]>(() => {
  const turns: ChatTurn[] = conversationMessages.value.map(item => ({ id: item.id, role: item.role === 'user' ? 'user' : 'assistant', content: item.content }))
  const known = new Set(turns.map(item => item.content.trim()))
  for (const entry of transcript.value) {
    if (entry.text && !known.has(entry.text.trim())) { turns.push({ id: `run-user-${entry.execution.run_id}`, role: 'user', content: entry.text }); known.add(entry.text.trim()) }
    const answer = entry.answer || entry.label
    if (answer && !known.has(answer.trim())) { turns.push({ id: `run-ai-${entry.execution.run_id}`, role: 'assistant', content: answer }); known.add(answer.trim()) }
  }
  return turns
})

const visibleConversations = computed(() => {
  const keyword = conversationSearch.value.trim().toLowerCase()
  if (!keyword) return conversations.value
  return conversations.value.filter((item) => item.title.toLowerCase().includes(keyword))
})

async function loadConversations(): Promise<void> {
  conversations.value = await getConversations('comic').catch(() => [])
}
async function startConversation(): Promise<void> {
  if (conversationBusy.value) return
  conversationBusy.value = true
  try {
    const created = await createConversation('guided', 'comic')
    activeConversationId.value = created.id
    conversationMessages.value = []
    await loadConversations()
    chatExpanded.value = true
    inspectorOpen.value = false
  } catch (failure) {
    error.value = failureText(failure)
  } finally {
    conversationBusy.value = false
  }
}

async function openConversation(id: string): Promise<void> {
  activeConversationId.value = id
  chatExpanded.value = true
  inspectorOpen.value = false
  await loadConversationMessages(id)
}

function beginConversationRename(item: Conversation): void {
  editingConversation.value = item.id
  conversationTitle.value = item.title
}

async function finishConversationRename(): Promise<void> {
  const id = editingConversation.value
  const title = conversationTitle.value.trim()
  editingConversation.value = ''
  if (!id || !title) return
  await renameConversation(id, title).catch((failure) => { error.value = failureText(failure) })
  await loadConversations()
}

async function removeConversation(id: string): Promise<void> {
  await deleteConversation(id).catch((failure) => { error.value = failureText(failure) })
  if (activeConversationId.value === id) { activeConversationId.value = ''; conversationMessages.value = [] }
  await loadConversations()
}
const assetFieldLabels: Record<string, string> = {
  appearance: '外观', clothing: '服装', traits: '特征', location: '地点', time: '时间', weather: '天气',
  lighting: '光影', atmosphere: '氛围', environment_features: '环境特点', visual_style: '视觉风格',
  color_palette: '色彩', materials: '材质', camera_language: '摄影语言', art_direction: '艺术方向',
}
const active = computed(() => selectDirectorExecution(executions.value, selectedRun.value, busy.value, previousRunIds.value))
const summary = computed(() => restoredSpec.value ? undefined : active.value?.director_execution_summary)
const spec = computed(() => restoredSpec.value ?? active.value?.director_spec ?? null)
const node = computed(() => summary.value?.stages.find(item => item.stage === selectedStage.value))
const hasRunning = computed(() => executions.value.some(item => ['running', 'pending'].includes(item.status) && !item.recovery_required))
const draftKey = computed(() => stageDraftKey(project.value?.project.project_id ?? '', spec.value?.schema_version === 2 ? `version:${spec.value.version}` : active.value?.run_id ?? '', spec.value?.schema_version === 2 ? 'director_assemble' : selectedStage.value))
const fields = computed(() => drafts.value[draftKey.value] ?? {})
const editing = computed(() => selectedStage.value !== 'director_assemble' && (spec.value?.schema_version === 2
  ? Object.keys(fields.value).some(key => key.startsWith(`${directorStageSections[selectedStage.value]}.`)) : draftKey.value in drafts.value))
const dirty = computed(() => Object.keys(drafts.value).length > 0)
const stale = computed(() => directorIsStale(spec.value, project.value?.creative_brief.version, project.value?.project.director_version, assets.value))
const confirmable = computed(() => canConfirmDirector(restoredSpec.value ? 'completed' : active.value?.status, spec.value, stale.value, dirty.value) && !busy.value && !hasRunning.value)
const confirmed = computed(() => confirmable.value && spec.value?.user_confirmed === true)
const editable = computed(() => spec.value?.schema_version === 2 ? !stale.value && editableDirectorNode(selectedStage.value) : !!node.value && editableDirectorNode(node.value.stage) && summary.value?.mode === 'professional' && !!summary.value?.available_actions.includes('edit_stage'))
const visibleNodes = computed(() => mode.value === 'fast' ? fastDirectorNodeLabels : directorNodeLabels)
const projectTitle = computed(() => workspaceProjectTitle(project.value?.project.title ?? '', project.value?.creative_brief.original_request ?? ''))
const title = computed(() => section.value === 'director' ? visibleNodes.value[selectedStage.value] : navigation.find(item => item.id === section.value)?.label)
const activeNavigation = computed(() => chatExpanded.value ? 'conversation' : section.value)
const activeRun = computed(() => !restoredSpec.value && active.value ? runs.value[active.value.run_id] : null)
const runningExecution = computed(() => executions.value.find(item => ['running', 'pending'].includes(item.status) && !item.recovery_required))
const canDispatch = computed(() => canDispatchDirectorInput({ busy: busy.value, running: hasRunning.value, loading: loading.value, error: !!error.value, cancelling: cancelling.value }))
const transcript = computed(() => chronologicalDirectorExecutions(executions.value, runs.value).map(item => {
  const run = runs.value[item.run_id]
  const text = String(run?.state.task ?? '')
  // 聊天里只保留用户自己的创作表达；重跑/节点名属于执行记录，不进对话。
  return { execution: item, text,
    answer: directorConversationSummary(item.director_spec), label: item.director_execution_summary.status_label }
}))
/* 执行记录（Run History）与创作对话分开：失败 / Trace 只在这里呈现。 */
const runHistory = computed(() => chronologicalDirectorExecutions(executions.value, runs.value).map(item => {
  const run = runs.value[item.run_id]
  return { run_id: item.run_id, status: item.status, label: `${item.director_execution_summary.mode === 'fast' ? '普通模式' : '专业模式'} · ${String(run?.state.task ?? item.run_id).slice(0, 24)}`,
    error: item.status === 'failed' ? (item.director_execution_summary.error_id ?? run?.error ?? '任务未完成') : '',
    trace: String(run?.state.trace_id ?? ''), resumable: item.recovery_required || !!summary.value?.available_actions.includes('resume') }
}))
function nodeState(stage: string): string {
  if (stale.value) return '已过期'
  if (restoredSpec.value) return '保存的方案'
  const actual = summary.value?.stages.find(item => item.stage === stage)
  return actual ? directorStateLabels[actual.status] ?? actual.status : spec.value ? '方案内容' : '未开始'
}
function nodeStatus(stage: string): string {
  return !restoredSpec.value && !stale.value ? summary.value?.stages.find(item => item.stage === stage)?.status ?? '' : ''
}
function openDraft(runId?: string): void {
  if (runId && runId !== active.value?.run_id) { selectedRun.value = runId; restoredSpec.value = null }
  visitStage('director_assemble')
}
function failureText(failure: unknown): string {
  if (failure instanceof CoreApiError) return `${failure.message}${failure.traceId ? ` · Trace：${failure.traceId}` : ''}`
  return failure instanceof Error ? failure.message : '操作未完成，请查看真实任务状态'
}
async function confirm(): Promise<void> {
  if (!confirmable.value || !project.value || !spec.value) return
  busy.value = true
  try { restoredSpec.value = await confirmDirectorVersion(project.value.project.project_id, Number(spec.value.version), project.value.project.current_version); await refresh() }
  catch (failure) { error.value = failureText(failure) }
  finally { busy.value = false }
}
function invalidate(): void { revisionVersion.value = null }
function beginEdit(): void {
  if (!editable.value || busy.value || hasRunning.value) return
  if (spec.value?.schema_version === 2) {
    const prefix = `${directorStageSections[selectedStage.value]}.`
    const selected = Object.fromEntries(Object.entries(directorDraftFields(spec.value)).filter(([key]) => key.startsWith(prefix)))
    drafts.value[draftKey.value] = { ...selected, ...fields.value }
    return
  }
  if (!node.value) return
  if (!editing.value) drafts.value[draftKey.value] = Object.fromEntries(Object.entries(Object.values(node.value.output)[0] ?? {})
    .filter(([key, value]) => key !== 'hard_constraints' && key in directorFieldLabels && (value == null || typeof value === 'string' || Array.isArray(value)))
    .map(([key, value]) => [key, Array.isArray(value) ? value.join('\n') : String(value ?? '')]))
  invalidate()
}
function editFinal(stage = 'creative_understanding'): void { visitStage(stage); beginEdit() }
function reviseByInstruction(): void {
  if (!spec.value?.version || stale.value || busy.value || hasRunning.value) return
  revisionVersion.value = Number(spec.value.version)
  chatExpanded.value = true
}
async function saveFinal(instruction?: string): Promise<void> {
  if (!project.value || !spec.value || busy.value || hasRunning.value) return
  const key = draftKey.value
  busy.value = true; error.value = ''
  try {
    restoredSpec.value = await saveDirectorDraft(project.value.project.project_id, {
      expected_project_version: project.value.project.current_version,
      expected_director_version: instruction ? revisionVersion.value : spec.value.version,
      conversation_id: await ensureConversation(),
      ...(instruction ? { revision_instruction: instruction } : { draft: editableDirectorDraft(spec.value, fields.value) }),
    })
    delete drafts.value[key]; revisionVersion.value = null
    await refresh()
  } catch (failure) { error.value = failureText(failure) }
  finally { busy.value = false }
}
async function reviewFinal(): Promise<void> {
  if (!spec.value || dirty.value) return
  await execute('', { review_current: true, expected_director_version: spec.value.version })
}
function updateField(key: string, value: string): void { if (editing.value) drafts.value[draftKey.value]![key] = value }
function discardNode(): void {
  if (spec.value?.schema_version !== 2) { delete drafts.value[draftKey.value]; return }
  const remaining = discardDirectorNodeDraft(fields.value, selectedStage.value)
  if (Object.keys(remaining).length) drafts.value[draftKey.value] = remaining
  else delete drafts.value[draftKey.value]
}
async function openReference(id: string): Promise<void> {
  if (referenceUrls.value[id] || referenceLoading.has(id)) return
  referenceLoading.add(id)
  try {
    const url = await getArtifactContentUrl(id)
    if (!url) throw new Error('参考图暂时无法载入')
    if (disposed) URL.revokeObjectURL(url)
    else referenceUrls.value[id] = url
  } catch (failure) { referenceErrors.value[id] = failureText(failure) }
  finally { referenceLoading.delete(id) }
}
async function refresh(): Promise<void> {
  if (!project.value || refreshing) return
  refreshing = true
  const id = project.value.project.project_id
  try {
    const [context, tasks, nextAssets] = await Promise.all([getComicProject(id), getDirectorExecutions(id), getComicAssets(id)])
    if (disposed || id !== project.value?.project.project_id) return
    if (context) project.value = context
    executions.value = tasks; assets.value = nextAssets
    const records = await Promise.all(tasks.filter(item => !runs.value[item.run_id] || runs.value[item.run_id]!.status !== item.status || ['pending', 'running'].includes(item.status)).map(item => getRun(item.run_id)))
    if (disposed || id !== project.value?.project.project_id) return
    records.forEach(run => { if (run) runs.value[run.id] = run })
    const latestVersion = context?.project.director_version
    if (!busy.value && !hasRunning.value && latestVersion && !tasks.some(item => item.director_spec?.version === latestVersion) && restoredSpec.value?.version !== latestVersion) {
      const current = await getCurrentDirector(id)
      if (!disposed && id === project.value?.project.project_id) restoredSpec.value = current
    }
    const associated = Object.values(runs.value).find(run => run.state.conversation_id)?.state.conversation_id
    if (associated && !conversationId.value) conversationId.value = String(associated)
    if (inspectorOpen.value && active.value) events.value = await getEvents(active.value.run_id)
  } finally { refreshing = false }
}
async function ensureConversation(): Promise<string> {
  const id = project.value!.project.project_id
  if (!conversationId.value) conversationId.value = localStorage.getItem(`kantoku-comic-conversation:${id}`) ?? ''
  if (!conversationId.value) conversationId.value = (await createConversation('guided', 'comic')).id
  localStorage.setItem(`kantoku-comic-conversation:${id}`, conversationId.value)
  return conversationId.value
}
async function execute(text: string, options: Record<string, unknown> = {}, selectedMode: CreationMode = mode.value): Promise<void> {
  if (busy.value || hasRunning.value || (!project.value && !text.trim())) { composer.value?.fill(text); return }
  const editingKey = draftKey.value
  busy.value = true; pendingText.value = text; previousRunIds.value = executions.value.map(item => item.run_id); error.value = ''; invalidate()
  restoredSpec.value = null
  try {
    if (!project.value) {
      project.value = await createComicProject(text)
      localStorage.setItem('kantoku-comic-project', project.value.project.project_id)
    }
    await refresh()
    const conversation = await ensureConversation()
    const result = await createDirectorExecution(project.value.project.project_id, {
      expected_project_version: project.value.project.current_version, creation_mode: selectedMode,
      creative_operation: 'new',
      conversation_id: conversation,
      ...('previous_run_id' in options || 'resume_run_id' in options ? {} : { task: text || null }), ...options,
    })
    if (disposed) return
    selectedRun.value = result.run_id; restoredSpec.value = null
    if ('stage_edits' in options) delete drafts.value[editingKey]
    await refresh()
    pendingText.value = ''
  } catch (failure) {
    error.value = failureText(failure)
    await refresh().catch(() => undefined)
    const failedRun = selectDirectorExecution(executions.value, '', true, previousRunIds.value)
    if (failedRun) { selectedRun.value = failedRun.run_id; pendingText.value = '' }
  } finally { busy.value = false }
}
function sendInput(text: string): void {
  if (!text.trim()) return
  chatExpanded.value = true
  void titleConversation(text)
  if (revisionVersion.value !== null) { void saveFinal(text); return }
  if (busy.value || hasRunning.value || cancelling.value || queuedInputs.value.length) {
    queuedInputs.value.push({ id: ++nextInputId, text, mode: mode.value })
    void dispatchQueued()
    return
  }
  void execute(text)
}
/* 新对话默认标题是占位名：用户发出第一条创作需求后，用需求本身命名，历史才可读。 */
async function titleConversation(text: string): Promise<void> {
  const id = activeConversationId.value
  const current = conversations.value.find(item => item.id === id)
  if (!id || !current || !/^新对话$/.test(current.title)) return
  const title = text.trim().replace(/\s+/g, ' ').slice(0, 24)
  if (!title) return
  await renameConversation(id, title).catch(() => {})
  await loadConversations()
}
async function dispatchQueued(explicit = false): Promise<void> {
  if (explicit) error.value = ''
  if (!canDispatch.value || disposed) return
  const next = queuedInputs.value.shift()
  if (next) await execute(next.text, {}, next.mode)
}
async function cancelExecution(): Promise<void> {
  const task = runningExecution.value
  if (!task || cancelling.value) return
  cancelling.value = true
  try { await cancelRun(task.run_id); await refresh() }
  catch (failure) { error.value = failureText(failure) }
  finally { cancelling.value = false }
}
async function rerun(save = false): Promise<void> {
  if (!active.value || !node.value || !summary.value) return
  mode.value = summary.value.mode
  const options: Record<string, unknown> = { previous_run_id: active.value.run_id, rerun_from: node.value.stage }
  if (save) {
    const patch = { ...Object.values(node.value.output)[0] }
    Object.entries(fields.value).forEach(([key, text]) => { patch[key] = Array.isArray(patch[key]) ? text.split('\n').map(item => item.trim()).filter(Boolean) : patch[key] == null && !text.trim() ? null : text })
    options.stage_edits = { [node.value.stage]: patch }
  }
  await execute('', options)
}
async function resume(): Promise<void> {
  if (!active.value || !summary.value) return
  mode.value = summary.value.mode; await execute('', { resume_run_id: active.value.run_id })
}
function regenerate(): void {
  const task = String(activeRun.value?.state.task ?? project.value?.creative_brief.original_request ?? '')
  if (task) void execute(task)
}
async function loadPage(): Promise<void> {
  const requestId = ++pageRequest
  const id = project.value?.project.project_id
  if (!id) return
  error.value = ''
  try {
    if (section.value === 'storyboard' || section.value === 'prompt') {
      const next = await getComicStoryboards(id)
      if (requestId !== pageRequest) return
      boards.value = next
      if (!next.some(item => item.storyboard_id === selectedBoard.value)) selectedBoard.value = next[0]?.storyboard_id ?? ''
      await loadShots()
    }
    if (section.value === 'history') {
      const next = await getDirectorVersions(id)
      if (requestId === pageRequest) versions.value = next
    }
  } catch (failure) { if (requestId === pageRequest) error.value = failureText(failure) }
}
async function loadShots(): Promise<void> {
  const id = selectedBoard.value
  if (!id) { shots.value = []; return }
  try {
    const next = await getComicShots(id)
    if (id !== selectedBoard.value || disposed) return
    shots.value = next
    if (!next.some(item => item.shot_id === selectedShot.value)) selectedShot.value = next[0]?.shot_id ?? ''
    if (section.value === 'prompt') await loadPrompts()
  } catch (failure) { error.value = failureText(failure) }
}
async function loadPrompts(): Promise<void> {
  const id = selectedShot.value
  prompts.value = []
  if (!id) return
  try {
    const next = await getComicPromptVersions(id)
    if (id === selectedShot.value && !disposed) prompts.value = next
  } catch (failure) { error.value = failureText(failure) }
}
async function compilePrompt(): Promise<void> {
  const shot = shots.value.find(item => item.shot_id === selectedShot.value)
  const board = boards.value.find(item => item.storyboard_id === selectedBoard.value)
  if (!project.value || !shot || !confirmed.value || board?.director_spec_version !== spec.value?.version || compiling.value || busy.value || hasRunning.value) return
  compiling.value = true; error.value = ''
  try {
    await compileComicPrompt(shot.shot_id, project.value.project.current_version, shot.version)
    await refresh(); await loadPrompts()
  } catch (failure) { error.value = failureText(failure) }
  finally { compiling.value = false }
}
async function restore(version: number): Promise<void> {
  if (!project.value || busy.value || hasRunning.value) return
  busy.value = true; invalidate()
  try {
    restoredSpec.value = await restoreDirectorVersion(project.value.project.project_id, version, project.value.project.current_version)
    await refresh(); await loadPage(); section.value = 'director'; selectedStage.value = 'director_assemble'
  } catch (failure) { error.value = failureText(failure) }
  finally { busy.value = false; restoreChoice.value = null }
}
function newProject(): void {
  if (busy.value || hasRunning.value || ((Object.keys(drafts.value).length || queuedInputs.value.length) && !window.confirm('放弃未保存的修改和待发送补充并新建作品？'))) return
  queuedInputs.value = []
  pageRequest++; project.value = null; executions.value = []; runs.value = {}; assets.value = []; boards.value = []; shots.value = []; versions.value = []; prompts.value = []
  selectedRun.value = ''; restoredSpec.value = null; drafts.value = {}; conversationId.value = ''; revisionVersion.value = null; error.value = ''; pendingText.value = ''
  section.value = 'director'; selectedStage.value = 'director_assemble'; chatExpanded.value = true; inspectorOpen.value = false; restoreChoice.value = null
  localStorage.removeItem('kantoku-comic-project')
  if (props.initialRunId) emit('newProject')
}
function visitStage(stage: string): void { section.value = 'director'; selectedStage.value = stage; chatExpanded.value = false }
function openSection(id: string): void {
  if (id === 'conversation') {
    chatExpanded.value = true
    inspectorOpen.value = false
    return
  }
  section.value = id
  chatExpanded.value = false
}
function scrollState(): void { const el = timeline.value; if (el) nearBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80 }
function previewReference(media: { url: string }): void { window.open(media.url, '_blank', 'noopener') }
watch(section, () => { void loadPage() })
watch(() => [route.value.workspacePage, route.value.directorStage], async ([page, stage]) => {
  applyingRoute = true
  chatExpanded.value = !page || page === 'conversation'
  if (page && page !== 'conversation') {
    section.value = page
    if (page === 'director') selectedStage.value = stage ?? 'director_assemble'
  }
  if (!loading.value && mode.value === 'fast' && selectedStage.value === 'director_critic') {
    navigate({ ...route.value, directorStage: 'director_assemble' }, true)
  }
  await nextTick()
  applyingRoute = false
}, { flush: 'sync' })
watch([section, selectedStage, chatExpanded], () => {
  if (applyingRoute || !['workspace', 'workspace_run'].includes(route.value.name)) return
  const workspacePage = chatExpanded.value ? 'conversation' : section.value
  const directorStage = workspacePage === 'director' ? selectedStage.value : undefined
  if (route.value.workspacePage !== workspacePage || route.value.directorStage !== directorStage) {
    navigate({ ...route.value, workspacePage, directorStage })
  }
})
watch(mode, value => {
  if (value === 'fast' && selectedStage.value === 'director_critic') selectedStage.value = 'director_assemble'
  if (project.value) sessionStorage.setItem(`kantoku-comic-view-mode:${project.value.project.project_id}`, value)
})
watch([canDispatch, () => queuedInputs.value.length], () => { void dispatchQueued() })
watch(() => `${project.value?.project.project_id}:${mode.value}:${section.value}:${selectedStage.value}:${selectedRun.value}:${spec.value?.version}:${chatExpanded.value}`, async (key, old) => {
  if (stageScroll.value) scrollPositions.set(old, stageScroll.value.scrollTop)
  await nextTick()
  if (stageScroll.value) stageScroll.value.scrollTop = scrollPositions.get(key) ?? 0
})
watch(inspectorOpen, async open => { if (open && active.value) { try { events.value = await getEvents(active.value.run_id) } catch (failure) { error.value = failureText(failure) } } })
watch(() => transcript.value.map(item => `${item.execution.run_id}:${item.execution.status}`).join('|') + pendingText.value + queuedInputs.value.length, async () => { if (!nearBottom.value) return; await nextTick(); timeline.value?.scrollTo({ top: timeline.value.scrollHeight, behavior: 'auto' }) })
onMounted(async () => {
  try {
    const legacy = props.initialRunId ? await getRun(props.initialRunId) : null
    legacyOnly.value = !!legacy && !legacy.state.project_id
    const id = String(legacy ? legacy.state.project_id ?? '' : localStorage.getItem('kantoku-comic-project') ?? '')
    if (id) {
      project.value = await getComicProject(id); await refresh()
      if (project.value) localStorage.setItem('kantoku-comic-project', id)
      selectedRun.value = executions.value.find(item => item.director_spec?.version === project.value?.project.director_version)?.run_id
        ?? (legacy?.workflow === 'comic.director' ? legacy.id : '')
      const viewMode = sessionStorage.getItem(`kantoku-comic-view-mode:${id}`)
      mode.value = viewMode === 'professional' || viewMode === 'fast' ? viewMode : active.value?.director_execution_summary.mode ?? 'fast'
      if (mode.value === 'fast' && selectedStage.value === 'director_critic') navigate({ ...route.value, directorStage: 'director_assemble' }, true)
    }
    if (legacy && legacy.workflow !== 'comic.director') { section.value = 'assets'; chatExpanded.value = false }
    await loadPage()
    await loadConversations()
    activeConversationId.value = conversations.value[0]?.id ?? ''
    await loadConversationMessages(activeConversationId.value)
  } catch (failure) { error.value = failureText(failure) }
  finally { loading.value = false }
  timer = setInterval(() => { void refresh().catch(failure => { error.value = failureText(failure) }) }, 2000)
})
onBeforeUnmount(() => { disposed = true; pageRequest++; if (timer) clearInterval(timer); Object.values(referenceUrls.value).forEach(url => URL.revokeObjectURL(url)) })
</script>

<template>
  <ComicWorkspaceShell v-model:navigation-open="navOpen" :navigation="navigation" :active-page="activeNavigation" :creating-conversation="conversationBusy" @select-page="openSection" @new-conversation="startConversation">
    <template #toolbar>
      <strong class="toolbar-project">{{ projectTitle }}</strong>
      <label class="toolbar-mode"><span>创作模式</span>
        <select v-model="mode" :disabled="busy || hasRunning || loading">
          <option value="fast">普通模式</option><option value="professional">专业导演模式</option>
        </select>
      </label>
      <span class="toolbar-status" role="status">{{ busy || hasRunning ? summary?.status_label ?? '正在生成' : '' }}</span>
      <button class="ui-button quiet sm toolbar-new" :disabled="busy || hasRunning" @click="newProject">新作品</button>
    </template>
    <template #recent>
      <input v-model="conversationSearch" class="history-search" type="search" placeholder="搜索对话" aria-label="搜索对话" />
      <div class="history-list" tabindex="0" aria-label="最近对话列表">
        <div v-for="item in visibleConversations" :key="item.id" class="conversation-row" :class="{ active: item.id === activeConversationId }">
          <input v-if="editingConversation === item.id" v-model="conversationTitle" class="history-search" aria-label="重命名对话" @keyup.enter="finishConversationRename" @keyup.esc="editingConversation = ''" />
          <template v-else>
            <button class="history-item" :title="item.title" :aria-current="item.id === activeConversationId ? 'true' : undefined" @click="openConversation(item.id)"><span>{{ item.title }}</span></button>
            <span class="conversation-tools">
              <button :aria-label="`重命名 ${item.title}`" @click="beginConversationRename(item)"><Pencil :size="13" /></button>
              <button :aria-label="`删除 ${item.title}`" @click="removeConversation(item.id)"><Trash2 :size="13" /></button>
            </span>
          </template>
        </div>
        <p v-if="!visibleConversations.length" class="history-empty">{{ conversationSearch ? '没有匹配的对话' : '暂无对话' }}</p>
      </div>
    </template>
            <template #heading>
              <div><small>{{ chatExpanded ? '同一个项目，同一段创作对话' : section === 'director' ? '导演工作区' : '项目工作区' }}</small><h2>{{ chatExpanded ? '与 AI 导演协作' : title }}</h2></div>
              <span v-if="!chatExpanded && section === 'director' && spec" class="node-version">v{{ spec.version }}<span v-if="dirty"> · 未保存修改</span></span>
              <button v-if="!chatExpanded" class="ui-button quiet sm" :aria-expanded="inspectorOpen" @click="inspectorOpen = !inspectorOpen"><PanelRight :size="15" /> 详情</button>
            </template>
        <div class="workspace-content">
          <main class="workspace-stage">

              <div v-if="!chatExpanded" :key="`${section}:${selectedStage}:${selectedRun}`" ref="stageScroll" class="stage-scroll" aria-label="当前节点工作区">
                <template v-if="section === 'director'">
<nav class="director-flow" aria-label="导演流程">
  <button v-for="(label, stage) in visibleNodes" :key="stage" :aria-current="selectedStage === stage ? 'step' : undefined" :data-status="nodeStatus(String(stage))" @click="visitStage(String(stage))">
    <span class="flow-marker"><Check v-if="nodeStatus(String(stage)) === 'completed'" :size="11" /></span>
    <span>{{ label }}<small v-if="mode === 'professional'">{{ nodeState(String(stage)) }}</small></span>
  </button>
</nav>
                  <p v-if="restoredSpec && mode === 'professional'" class="pane-note">查看当前保存的版本；节点没有重新执行，不沿用其他版本的执行状态。</p>
                  <p v-if="summary?.mode === 'fast' && mode === 'professional' && selectedStage !== 'director_assemble'" class="pane-note">显示已保存方案的对应内容；此任务未公开逐节点执行记录。</p>
                  <p v-if="selectedStage === 'cinematography' && (spec?.cinematography as { status?: string })?.status && (spec?.cinematography as { status?: string })?.status !== 'complete'" class="review-notice">摄影方案待补充或调整。已保留真实草稿，不会自动进入制作。</p>
                  <DirectorNodeView :mode="mode" :stage="selectedStage" :node="node" :spec="spec" :fields="fields" :editing="editing" :editable="editable" :rerunnable="mode === 'professional' && !restoredSpec && !!node && !!summary?.available_actions.includes('rerun_stage') && !dirty" :busy="busy || hasRunning" :critic="summary?.critic_result" @edit="beginEdit" @field="updateField" @cancel="discardNode" @save="spec?.schema_version === 2 ? saveFinal() : rerun(true)" @rerun="rerun()" @revise="editFinal('visual_direction')" @open="visitStage" />
                  <div v-if="spec && selectedStage === 'director_assemble'" class="final-approval"><p>{{ stale ? '来源已变化，需要更新方案' : confirmed ? '当前版本已确认' : dirty ? '请先保存编辑，再检查并确认' : '这是可修改的草稿，确认后才进入下一阶段。' }}</p><div class="draft-actions"><button class="ui-button sm" :disabled="busy || hasRunning || stale || dirty" @click="reviewFinal">检查当前方案</button><button class="ui-button quiet sm" :disabled="busy || hasRunning || stale || dirty" @click="reviseByInstruction">用对话修改</button><button class="ui-button quiet sm" :disabled="busy || hasRunning || dirty" @click="regenerate">重新生成</button><button class="ui-button primary sm" :disabled="!confirmable || confirmed" @click="confirm">确认最终方案</button><button class="ui-button sm" :disabled="!confirmed" @click="openSection('prompt')">进入下一步</button></div></div>
                  <button v-if="spec && selectedStage !== 'director_assemble' && !editing && selectedStage !== 'director_critic'" class="ui-button quiet sm" @click="visitStage('director_assemble')">{{ mode === 'fast' ? '查看整体方案并确认' : '返回最终导演稿' }}</button>
                  <p v-if="spec?.schema_version !== 2 && spec" class="pane-note">这是旧版方案，仅保留历史查看。请在对话中重新生成 v2 导演方案。</p>
                </template>
                <template v-else-if="section === 'assets'">
                  <section v-for="(label, kind) in { character: '角色资产', scene: '场景资产', style: '风格资产' }" :key="kind" class="asset-group"><h3>{{ label }}</h3>
                    <details v-for="asset in assets.filter(item => item.details.kind === kind && item.state !== 'deleted')" :key="asset.asset_id"><summary>{{ asset.name }} <small>v{{ asset.version }}{{ asset.pinned_version ? ` · 锁定 v${asset.pinned_version}` : '' }}</small></summary><dl><div v-for="(value, key) in Object.fromEntries(Object.entries(asset.details).filter(([key]) => key !== 'kind'))" :key="key"><dt>{{ assetFieldLabels[key] ?? key }}</dt><dd>{{ Array.isArray(value) ? value.join('；') : value }}</dd></div></dl><p>固定约束：{{ asset.fixed_constraints.join('；') || '未指定' }}</p><p>参考图：{{ asset.reference_artifact_ids.length }} 张</p></details>
                    <p v-if="!assets.some(item => item.details.kind === kind)" class="pane-note">暂无{{ label }}。</p>
                  </section>
                  <h3>参考素材</h3><div v-for="id in [...new Set(assets.flatMap(asset => asset.reference_artifact_ids))]" :key="id"><button class="ui-button quiet sm" @click="openReference(id)">查看参考图 · {{ id }}</button><ChatImageAttachment v-if="referenceUrls[id]" :media="{ url: referenceUrls[id]!, filename: `reference-${id}.png` }" @open="previewReference" /><p v-if="referenceErrors[id]" role="alert">{{ referenceErrors[id] }}</p></div>
                  <p class="pane-note">参考素材沿用资产里的真实 Artifact 引用；本轮没有新增上传或资产生产能力。</p>
                  <h3>生成结果</h3><slot name="works" />
                </template>
                <template v-else-if="section === 'storyboard' || section === 'prompt'">
                  <p v-if="section === 'prompt' && !confirmed" class="review-notice">导演方案尚未在本界面确认。可查看历史 Prompt，但不能进入新制作。</p>
                  <label v-if="boards.length">分镜 <select v-model="selectedBoard" @change="loadShots"><option v-for="board in boards" :key="board.storyboard_id" :value="board.storyboard_id">{{ board.title }} · v{{ board.version }}</option></select></label>
                  <p v-else class="pane-note">尚无作品分镜。此页面只展示已有分镜，不会自动开始制作。</p>
                  <template v-if="section === 'storyboard'"><article v-for="shot in shots" :key="shot.shot_id" class="shot-row"><strong>镜头 {{ shot.sequence_number }} · {{ shot.subject }}</strong><p>{{ shot.purpose }} · {{ shot.action }}</p><small>v{{ shot.version }} · {{ directorStateLabels[shot.status] ?? shot.status }} · 角色 {{ shot.character_asset_versions.map(ref => `${ref.asset_id} v${ref.version}`).join('、') || '未引用' }}</small></article></template>
                  <template v-else><label v-if="shots.length">镜头 <select v-model="selectedShot" @change="loadPrompts"><option v-for="shot in shots" :key="shot.shot_id" :value="shot.shot_id">{{ shot.sequence_number }} · {{ shot.subject }}</option></select></label><details v-for="prompt in prompts" :key="String(prompt.prompt_id) + prompt.version"><summary>Prompt v{{ prompt.version }} · {{ prompt.model_target }}</summary><p>{{ prompt.positive_prompt }}</p><h3>负向约束</h3><p>{{ prompt.negative_prompt }}</p><small>导演 v{{ prompt.director_spec_version }} · 镜头 v{{ prompt.shot_version }} · {{ prompt.compiler_version }}</small></details><p v-if="selectedShot && !prompts.length" class="pane-note">当前镜头没有已保存的 Prompt 版本。</p><button v-if="selectedShot" class="ui-button primary sm" :disabled="!confirmed || compiling || busy || hasRunning || boards.find(board => board.storyboard_id === selectedBoard)?.director_spec_version !== spec?.version" @click="compilePrompt">{{ compiling ? '正在编译 Prompt' : '编译当前镜头 Prompt' }}</button><p v-if="selectedShot && boards.find(board => board.storyboard_id === selectedBoard)?.director_spec_version !== spec?.version" class="pane-note">分镜引用的导演版本与当前方案不同。请先更新分镜，旧 Prompt 可继续查看。</p><button class="ui-button sm" :disabled="!confirmed" @click="section = 'assets'">查看生成结果</button></template>
                </template>
                <template v-else-if="section === 'history'">
                  <h3>方案版本</h3><article v-for="version in versions" :key="Number(version.version)" class="history-row"><header><div><strong>导演方案 v{{ version.version }}</strong><small>{{ version.created_at }}</small></div><button class="ui-button sm" :disabled="busy || hasRunning" @click="restoreChoice = Number(version.version)">恢复为新版本</button></header><details><summary>查看版本摘要</summary><AssistantMessageBlock :content="directorSummary(version, mode) || '旧版方案（只读）'" :show-mark="false" /></details><div v-if="restoreChoice === Number(version.version)" class="review-notice" role="status"><p>将 v{{ version.version }} 恢复为新版本。历史保留，当前确认状态会清除。</p><button class="ui-button primary sm" :disabled="busy || hasRunning" @click="restore(Number(version.version))">确认恢复</button><button class="ui-button quiet sm" :disabled="busy" @click="restoreChoice = null">取消</button></div></article><p v-if="!versions.length" class="pane-note">暂无已保存方案版本。</p>
                  <h3>真实执行记录</h3><button v-for="item in executions" :key="item.run_id" class="history-task" @click="selectedRun = item.run_id; restoredSpec = null; visitStage('director_assemble')">{{ item.director_execution_summary.mode === 'fast' ? '普通模式' : '专业模式' }} · {{ directorStateLabels[item.status] ?? item.status }}<small>{{ item.run_id }}</small></button>
                </template>
              </div>
            <section v-show="chatExpanded" ref="timeline" class="workspace-messages" aria-label="连续创作对话" @scroll="scrollState">
              <p v-if="!chatTurns.length && !pendingText" class="conversation-welcome">描述你的故事、人物或希望观众感受到的情绪。我们从创作理解开始，再一起确认导演方案。</p>
              <article v-for="turn in chatTurns" :key="turn.id" class="creative-turn">
                <UserMessageBubble v-if="turn.role === 'user'" :content="turn.content" />
                <AssistantMessageBlock v-else :content="turn.content" :show-mark="false" />
              </article>
              <div v-if="!restoredSpec && active?.director_spec" class="draft-actions"><button class="ui-button sm" @click="openDraft(active.run_id)">进入导演方案 <ArrowRight :size="15" /></button><span>{{ stale ? '来源已变化' : confirmed ? '已确认' : '待确认草稿' }}</span></div>
              <UserMessageBubble v-if="pendingText && !transcript.some(entry => !previousRunIds.includes(entry.execution.run_id) && entry.text === pendingText)" :content="pendingText" :pending="busy ? 'replying' : 'failed'" />
              <p v-if="busy && !active" role="status">正在提交创意，等待真实执行状态…</p>
              <p v-if="active?.status === 'cancelled'" class="pane-note" role="status">任务已取消。正在进行的模型请求可能仍需结束，但不会继续下一节点或保存导演方案。</p>
              <article v-for="input in queuedInputs" :key="input.id" class="queued-turn">
                <UserMessageBubble :content="input.text" />
                <div class="queued-note"><span>补充已排队 · 当前方案结束后处理 · {{ input.mode === 'professional' ? '专业模式' : '普通模式' }}</span><button class="ui-button quiet sm" @click="queuedInputs = queuedInputs.filter(item => item.id !== input.id)">撤回</button></div>
              </article>
              <button v-if="queuedInputs.length && error && !busy && !hasRunning" class="ui-button sm" @click="dispatchQueued(true)">继续处理已发送的补充</button>
              <p v-if="error" class="workspace-error" role="alert">{{ error }}</p>
              <p v-if="dirty" class="pane-note">有未保存的节点修改。切换节点会保留草稿；保存并重新审核后才能确认。</p>
              <template v-if="restoredSpec"><AssistantMessageBlock :content="directorConversationSummary(restoredSpec)" :show-mark="false" /><div class="draft-actions"><span>方案 v{{ restoredSpec.version }} · {{ confirmed ? '已确认' : '草稿' }}</span><button class="ui-button sm" @click="openDraft()">进入导演方案 <ArrowRight :size="15" /></button></div></template>
              <button v-if="spec && !confirmable && !stale && !dirty" class="ui-button sm" :disabled="busy || hasRunning" @click="reviewFinal">审核当前草稿</button>
              <p v-if="confirmed" class="pane-note">当前版本已确认并保存到后端。编辑或恢复为新版本后，需要重新审核与确认。</p>
            </section>
            </main>
            <aside v-if="inspectorOpen && !chatExpanded" class="workspace-inspector" aria-label="节点详情">
              <header><strong>节点详情</strong><button class="ui-button quiet sm" aria-label="关闭详情" @click="inspectorOpen = false"><X :size="15" /></button></header>
              <dl><div><dt>方案版本</dt><dd>{{ spec?.version ?? '未生成' }}</dd></div><div><dt>Brief</dt><dd>{{ spec?.creative_brief_version ?? '未关联' }}</dd></div><div v-for="(version, key) in node?.input_versions ?? activeRun?.state.input_versions ?? {}" :key="String(key)"><dt>{{ key }}</dt><dd>v{{ version }}</dd></div><div><dt>来源资产</dt><dd v-for="(version, id) in spec?.asset_versions ?? {}" :key="String(id)">{{ id }} · v{{ version }}</dd><dd v-if="!Object.keys(spec?.asset_versions as object ?? {}).length">未绑定</dd></div><div><dt>知识引用</dt><dd>{{ Array.isArray(spec?.knowledge_refs) ? spec.knowledge_refs.join('、') || '未记录' : '未记录' }}</dd></div><div><dt>Run</dt><dd>{{ activeRun?.id ?? '本版本未关联' }}</dd></div><div><dt>Trace</dt><dd>{{ activeRun?.state.trace_id ?? '本版本未关联' }}</dd></div><div><dt>错误</dt><dd>{{ activeRun?.error ?? summary?.error_id ?? '无' }}</dd></div></dl>
              <details v-if="!restoredSpec"><summary>公开执行事件 · {{ events.length }}</summary><p v-for="event in events" :key="event.id">{{ event.event_type }}</p></details>
              <section class="inspector-runs" aria-label="任务状态">
                <strong>任务状态</strong>
                <div v-if="runHistory.length" class="run-list">
                  <article v-for="item in runHistory" :key="item.run_id" class="run-row" :data-status="item.status">
                    <header><span>{{ item.label }}</span><span class="run-status">{{ directorStateLabels[item.status] ?? item.status }}</span></header>
                    <p v-if="item.error" class="run-error">{{ item.error }}</p>
                    <p v-if="item.trace" class="run-trace">Trace：{{ item.trace }}</p>
                    <button v-if="item.resumable" class="ui-button sm" :disabled="busy || hasRunning" @click="resume">重新执行</button>
                  </article>
                </div>
                <p v-else class="inspector-empty">暂无执行记录。</p>
              </section>
            </aside>
        </div>

        <template #composer>
<div class="workspace-composer">
          <div class="composer-context"><button class="ui-button quiet sm" :aria-expanded="chatExpanded" @click="chatExpanded = !chatExpanded"><ArrowLeft v-if="chatExpanded" :size="15" /><MessageSquareText v-else :size="15" />{{ chatExpanded ? '返回工作区' : '打开创作对话' }}</button><small>{{ revisionVersion !== null ? `修改当前草稿 v${revisionVersion}` : '当前项目的同一个对话' }}</small><button v-if="revisionVersion !== null" class="ui-button quiet sm" @click="revisionVersion = null">取消修改</button><button v-else-if="spec" class="ui-button quiet sm" :disabled="busy || hasRunning || stale || dirty" @click="reviseByInstruction">修改当前方案</button></div>
          <p v-if="legacyOnly" class="pane-note">此历史任务没有作品级 Project；点击"新作品"进入作品级创作。</p>
          <div v-if="busy || hasRunning" class="execution-controls" role="status"><span>{{ summary?.status_label ?? '正在生成导演方案' }} · 可继续输入</span><button v-if="runningExecution" class="ui-button quiet sm" :disabled="cancelling" @click="cancelExecution">{{ cancelling ? '正在取消' : '取消当前任务' }}</button></div>
          <p v-if="!chatExpanded && error" class="workspace-error" role="alert">{{ error }}</p>
          <MessageComposer ref="composer" :disabled="loading || legacyOnly" @send="sendInput" />
          <small>{{ revisionVersion !== null ? '发送将保存当前方案的新修订；不会创建新创意。' : queuedInputs.length ? '补充已排队，刷新会丢失未执行补充；可在对话中撤回。' : '方案先保存为草稿，审核并确认后再进入制作。' }}</small>
        </div>
</template>
  </ComicWorkspaceShell>
</template>

<style scoped>
.toolbar-project { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; font-size:13px; font-weight:600; }
.toolbar-mode { display:flex; align-items:center; gap:8px; color:var(--text-secondary); font-size:12px; flex-shrink:0; }
.toolbar-status { color:var(--text-secondary); font-size:11px; max-width:140px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.toolbar-new { flex-shrink:0; }
select { color:var(--text-primary); background:transparent; border:1px solid var(--border-muted); border-radius:8px; padding:5px 8px; font:inherit; cursor:pointer; }
:deep(.comic-shell-heading) > div:first-child { flex:1; min-width:0; }
:deep(.comic-shell-heading) small { font-size:11px; color:var(--text-muted); }
:deep(.comic-shell-heading) h2 { font-size:16px; font-weight:600; margin:3px 0 0; }
.node-version { font-size:11px; color:var(--text-muted); }
.history-search { flex-shrink:0; width:100%; padding:7px 10px; margin-bottom:6px; border:0; border-radius:7px; background:transparent; color:var(--text-primary); font:inherit; font-size:12px; }
.history-search:focus { background:var(--surface); }
.history-list { flex:1; min-height:0; overflow-y:auto; overscroll-behavior:contain; scrollbar-width:thin; }
.conversation-row { position:relative; display:flex; align-items:center; min-height:34px; border-radius:7px; }
.conversation-row:hover, .conversation-row.active { background:var(--sidebar-selected); }
.history-item { flex:1; min-width:0; padding:9px 10px; border:0; background:transparent; color:var(--text-primary); font:inherit; font-size:12px; text-align:left; cursor:pointer; }
.history-item span { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.conversation-tools { display:none; gap:2px; flex-shrink:0; padding-right:6px; }
.conversation-row:hover .conversation-tools, .conversation-row:focus-within .conversation-tools { display:flex; }
.conversation-tools button { display:grid; place-items:center; width:22px; height:24px; border:0; border-radius:5px; background:transparent; color:var(--text-secondary); cursor:pointer; }
.conversation-tools button:hover { background:var(--surface); }
.conversation-row > input { margin:0; }
.history-empty { padding:6px 10px; color:var(--text-muted); font-size:12px; }
.workspace-content { display:flex; flex:1; min-width:0; min-height:0; }
.workspace-stage { display:flex; flex:1; min-width:0; min-height:0; flex-direction:column; }
.stage-scroll { flex:1; min-height:0; overflow:auto; padding:18px max(32px, calc((100% - 760px) / 2)); font-size:14px; line-height:1.65; animation:node-enter 140ms ease-out; }
.stage-scroll h3 { font-size:15px; font-weight:600; margin:24px 0 16px; }
@keyframes node-enter { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:translateY(0); } }
@media(prefers-reduced-motion:reduce) { .stage-scroll { animation:none; } }
.director-flow { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 24px; }
.director-flow button { display:flex; align-items:center; gap:7px; padding:7px 10px; border:0; border-radius:7px; background:transparent; color:var(--text-secondary); font:inherit; font-size:12px; text-align:left; cursor:pointer; }
.director-flow button:hover { background:var(--surface-subtle); }
.director-flow button[aria-current] { color:var(--accent); background:var(--accent-soft); }
.director-flow small { display:block; font-size:10px; color:var(--text-muted); margin-top:2px; }
.flow-marker { display:grid; place-items:center; flex-shrink:0; width:12px; height:12px; border:1px solid var(--border-strong); border-radius:50%; }
.director-flow button[aria-current] .flow-marker { border-color:var(--accent); }
.workspace-inspector { width:220px; padding:16px; overflow:auto; border-left:1px solid var(--border-muted); font-size:12px; background:var(--surface-subtle); }
.workspace-inspector header { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }
.workspace-inspector dt { color:var(--text-secondary); margin-top:12px; }
.workspace-inspector dd { margin:4px 0 0; overflow-wrap:anywhere; }
.inspector-runs { margin-top:18px; display:grid; gap:6px; }
.inspector-runs > strong { font-size:11px; color:var(--text-secondary); }
.run-list { display:grid; gap:6px; }
.run-row { padding:8px 0; display:grid; gap:4px; }
.run-row header { display:flex; justify-content:space-between; gap:8px; font-size:12px; }
.run-status { font-size:11px; color:var(--text-secondary); }
.run-row[data-status="failed"] .run-status, .run-error { color:var(--danger); }
.run-error { margin:0; font-size:11px; }
.run-trace { margin:0; font-size:10px; color:var(--text-muted); overflow-wrap:anywhere; }
.workspace-messages { flex:1; min-height:0; overflow:auto; padding:20px max(24px, calc((100% - 760px) / 2)); }
.creative-turn { margin-bottom:28px; }
.conversation-welcome { color:var(--text-secondary); font-size:14px; line-height:1.8; max-width:600px; padding:40px 0; }
.workspace-composer { background:transparent; }
.composer-context { display:flex; align-items:center; justify-content:space-between; gap:8px; min-height:28px; margin-bottom:6px; }
.composer-context small, .workspace-composer > small { color:var(--text-muted); font-size:11px; }
.workspace-composer > small { display:block; margin-top:8px; text-align:center; }
.execution-controls, .queued-note { display:flex; align-items:center; justify-content:space-between; gap:8px; color:var(--text-secondary); font-size:12px; }
.execution-controls { margin-bottom:8px; }
.queued-note { justify-content:flex-end; margin:8px 0 16px; }
.draft-actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:16px 0; font-size:12px; }
.draft-actions span { display:flex; gap:5px; align-items:center; color:var(--text-secondary); }
.final-approval { margin-top:24px; padding:20px 0; }
.final-approval > p { color:var(--text-secondary); font-size:13px; margin:0 0 12px; }
.final-approval .draft-actions { margin:0; }
.asset-group, .shot-row, .history-row { padding-bottom:16px; margin-bottom:20px; border-bottom:1px solid var(--border-muted); }
.asset-group h3 { margin-top:0; }
.history-row header { display:flex; gap:12px; align-items:center; justify-content:space-between; }
.history-row small { display:block; color:var(--text-muted); font-size:11px; margin-top:4px; }
.history-task { display:block; background:transparent; color:var(--text-primary); border:0; border-radius:8px; padding:12px 10px; margin:4px 0; width:100%; text-align:left; cursor:pointer; }
.history-task:hover { background:var(--surface-subtle); }
.history-task small { display:block; color:var(--text-muted); margin-top:4px; overflow-wrap:anywhere; }
.review-notice { color:var(--text-secondary); border-left:2px solid var(--accent); padding:8px 14px; margin:16px 0; }
.workspace-error { color:var(--danger); overflow-wrap:anywhere; margin:8px 0; }
button:focus-visible, select:focus-visible, textarea:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
@media(max-width:900px) { .toolbar-status { display:none; } .workspace-inspector { width:180px; } .stage-scroll { padding-inline:24px; } }
@media(max-width:600px) { .toolbar-mode > span, .toolbar-new, .composer-context > small, .node-version { display:none; } .stage-scroll, .workspace-messages { padding-inline:16px; } .workspace-inspector { width:140px; } .director-flow { gap:2px; } .director-flow button { padding:6px; } }
</style>
