<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Clapperboard, Layers, History, FileText, Film, MessageSquareText, PanelRight, ArrowLeft, ArrowRight, X, Check } from 'lucide-vue-next'
import MessageComposer from './chat/MessageComposer.vue'
import UserMessageBubble from './chat/UserMessageBubble.vue'
import AssistantMessageBlock from './chat/AssistantMessageBlock.vue'
import WorkspaceShell from './layout/WorkspaceShell.vue'
import ConversationHistory from './chat/ConversationHistory.vue'
import { conversationTaskTitle, ownsConversationRun } from './chat/conversationPresentation'
import DirectorNodeView from './DirectorNodeView.vue'
import ChatImageAttachment from './chat/ChatImageAttachment.vue'
import ImageGenerationPlaceholder from './chat/ImageGenerationPlaceholder.vue'
import { comicProductionProgress } from '../domains/comic/productionProgress'
import { quickDirectorMessage } from '../domains/comic/directorPresentation'
import { resumeRun, saveComicPrompt } from '../services/core'
import { navigate, route } from '../router'
import { canConfirmDirector, canDispatchDirectorInput, chronologicalDirectorExecutions, directorConversationSummary, directorDraftFields, discardDirectorNodeDraft, editableDirectorDraft, directorFieldLabels, directorIsStale, directorNodeLabels, directorStageSections, fastDirectorNodeLabels, directorStateLabels, directorSummary, editableDirectorNode, selectDirectorExecution, stageDraftKey } from '../domains/comic/directorPresentation'
import { CoreApiError, cancelRun, compileComicPrompt, confirmDirectorVersion, saveDirectorDraft, createComicProject, createConversation, createDirectorExecution, createRun, deleteConversation, getArtifactContentUrl, getComicAssets, getComicProject, getComicPromptVersions, getComicShots, getComicStoryboards, getConversation, getConversations, getDirectorExecutions, getDirectorVersions, getEvents, getRun, renameConversation, restoreDirectorVersion, type ComicAssetView, type ComicProjectContext, type ComicShotView, type ComicStoryboardView, type CreationMode, type DirectorExecution, type RuntimeEvent } from '../services/core'
import type { Conversation, ConversationMessage } from '../types'
import type { CoreRun } from '../types'

const props = defineProps<{ initialRunId?: string }>()
defineEmits<{ newProject: [] }>()
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
const compiling = ref(false)
const submittingImage = ref(false)
const manualDirectorApproval = ref(false)
const promptEdit = ref<{ shotId: string; version: number; director_summary: string; positive_prompt: string; negative_prompt: string } | null>(null)
const productionRun = ref<CoreRun | null>(null)
const productionEvents = ref<RuntimeEvent[]>([])
let productionAdvanced = false
let pendingCreation: { text: string; mode: CreationMode; requestId: string } | null = null
const productionRequests = new Map<string, string>()
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
let conversationEpoch = 0
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
const conversationBusy = ref(false)
const activeConversationId = ref('')
/* Conversation（用户 ↔ AI 聊天）与 Run History（执行记录）是两套数据，这里只装聊天。 */
const conversationMessages = ref<ConversationMessage[]>([])
interface ChatTurn { id: string; role: 'user' | 'assistant'; content: string; artifactId?: string }
async function loadConversationMessages(id: string): Promise<void> {
  if (!id) { conversationMessages.value = []; return }
  const loaded = await getConversation(id).catch(() => null)
  if (activeConversationId.value !== id) return
  conversationMessages.value = (loaded?.messages ?? []).filter(item => item.role !== 'system' && item.content.trim())
  await Promise.all(conversationMessages.value.filter(item => item.artifact_id).map(item => openReference(item.artifact_id!)))
}
const chatTurns = computed<ChatTurn[]>(() => {
  const turns: ChatTurn[] = conversationMessages.value.map(item => ({ id: item.id, role: item.role === 'user' ? 'user' : 'assistant', content: item.role === 'assistant' && item.type === 'plan' ? quickDirectorMessage(item.content) : item.content, artifactId: item.artifact_id ?? undefined }))
  const knownRuns = new Set(conversationMessages.value.map(item => item.run_id).filter(Boolean))
  for (const entry of transcript.value) {
    if (Object.values(runs.value).some(run => (run.state.quick_creation as Record<string, unknown> | undefined)?.director_run_id === entry.execution.run_id)) continue
    if (knownRuns.has(entry.execution.run_id)) continue
    if (entry.text) turns.push({ id: `run-user-${entry.execution.run_id}`, role: 'user', content: entry.text })
    const answer = entry.answer || entry.label
    if (answer) turns.push({ id: `run-ai-${entry.execution.run_id}`, role: 'assistant', content: answer })
  }
  return turns
})

async function loadConversations(): Promise<void> {
  conversations.value = (await getConversations('comic')).filter(item => item.interaction_mode === 'guided')
}
async function startConversation(): Promise<void> {
  if (conversationBusy.value) return
  conversationBusy.value = true
  try {
    const created = await createConversation('guided', 'comic')
    conversations.value.unshift(created)
    await openConversation(created.id)
    await loadConversations()
  } catch (failure) {
    error.value = failureText(failure)
  } finally {
    conversationBusy.value = false
  }
}

async function openConversation(id: string): Promise<void> {
  const epoch = ++conversationEpoch
  resetConversationView()
  activeConversationId.value = id
  localStorage.setItem('kantoku-comic-active-conversation', id)
  navigate({ ...route.value, conversationId: id, workspacePage: 'conversation', directorStage: undefined }, true)
  chatExpanded.value = true
  inspectorOpen.value = false
  loading.value = true
  try {
    const detail = await getConversation(id)
    if (epoch !== conversationEpoch || disposed) return
    if (!detail) throw new Error('无法读取这条对话，请重试')
    conversationMessages.value = (detail.messages ?? []).filter(item => item.role !== 'system')
    const related = await Promise.all((detail.related_run_ids ?? []).map(getRun))
    if (epoch !== conversationEpoch || disposed) return
    const owned = related.filter((run): run is CoreRun => !!run && ownsConversationRun(run, id))
    owned.forEach(run => { runs.value[run.id] = run })
    const latest = owned.find(run => (run.state.project_id && run.workflow.startsWith('comic.director')) || (run.workflow === 'comic.production.v1' && run.state.quick_creation))
    productionRun.value = latest?.workflow === 'comic.production.v1' ? latest : owned.find(run => (run.state.quick_creation as Record<string, unknown> | undefined)?.director_run_id === latest?.id) ?? null
    manualDirectorApproval.value = !!latest && (!productionRun.value || (productionRun.value.state.quick_creation as Record<string, unknown>)?.approval_required === true)
    const productionProject = (productionRun.value?.state.quick_creation as Record<string, unknown> | undefined)?.project_id
    if (latest || productionProject) {
      const context = await getComicProject(String(productionProject ?? latest?.state.project_id))
      if (epoch !== conversationEpoch || disposed) return
      project.value = context
      await refresh()
      if (epoch !== conversationEpoch || disposed) return
      selectedRun.value = executions.value[0]?.run_id ?? ''
      mode.value = active.value?.director_execution_summary.mode ?? 'fast'
    }
    await nextTick()
    if (epoch === conversationEpoch) composer.value?.fill('')
  } catch (failure) { if (epoch === conversationEpoch) error.value = failureText(failure) }
  finally { if (epoch === conversationEpoch) loading.value = false }
}

async function finishConversationRename(id: string, title: string): Promise<void> {
  await renameConversation(id, title).catch((failure) => { error.value = failureText(failure) })
  await loadConversations()
}

async function removeConversation(id: string): Promise<void> {
  if (!window.confirm('删除这条对话？关联作品、任务、产物和费用记录会保留。')) return
  try {
    await deleteConversation(id)
    await loadConversations()
    if (activeConversationId.value === id) {
      if (conversations.value[0]) await openConversation(conversations.value[0].id)
      else await startConversation()
    }
  } catch (failure) { error.value = failureText(failure) }
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
const hasRunning = computed(() => ['running', 'pending'].includes(productionRun.value?.status ?? '') || executions.value.some(item => ['running', 'pending'].includes(item.status) && !item.recovery_required))
const productionProgress = computed(() => productionRun.value ? comicProductionProgress(productionRun.value, productionEvents.value, !!productionRun.value.state.image_artifact_id) : null)
const executionLabel = computed(() => hasRunning.value
  ? productionProgress.value?.label ?? summary.value?.status_label ?? '正在执行'
  : busy.value ? '正在处理当前操作' : '')
const imageExecution = computed(() => productionRun.value?.image_execution)
const imageElapsed = computed(() => imageExecution.value?.started_at && imageExecution.value.finished_at
  ? Math.max(0, Math.round((Date.parse(imageExecution.value.finished_at) - Date.parse(imageExecution.value.started_at)) / 1000)) : null)
const draftKey = computed(() => stageDraftKey(project.value?.project.project_id ?? '', spec.value?.schema_version === 2 ? `version:${spec.value.version}` : active.value?.run_id ?? '', spec.value?.schema_version === 2 ? 'director_assemble' : selectedStage.value))
const fields = computed(() => drafts.value[draftKey.value] ?? {})
const editing = computed(() => selectedStage.value !== 'director_assemble' && (spec.value?.schema_version === 2
  ? Object.keys(fields.value).some(key => key.startsWith(`${directorStageSections[selectedStage.value]}.`)) : draftKey.value in drafts.value))
const dirty = computed(() => Object.keys(drafts.value).length > 0)
const stale = computed(() => directorIsStale(spec.value, project.value?.creative_brief.version, project.value?.project.director_version, assets.value))
const confirmable = computed(() => canConfirmDirector(restoredSpec.value ? 'completed' : active.value?.status, spec.value, stale.value, dirty.value) && !busy.value && !hasRunning.value)
// Durable approval is independent of the transient button's busy/running state.
const confirmed = computed(() => !stale.value && !dirty.value && spec.value?.user_confirmed === true
  && (spec.value.approval as { status?: string } | undefined)?.status === 'approved')
const productionEligible = computed(() => {
  if (!spec.value || spec.value.schema_version !== 2 || stale.value || dirty.value) return false
  const review = spec.value.critic_result as Record<string, unknown> | undefined
  const camera = spec.value.cinematography as Record<string, unknown> | undefined
  const findings = review?.findings as Record<string, unknown>[] | undefined
  return camera?.status === 'complete' && ['pass', 'needs_revision'].includes(String(review?.verdict))
    && typeof review?.reviewed_spec_hash === 'string'
    && !findings?.some(item => item.severity === 'error' || item.code === 'REVIEW_EXECUTION_FAILED')
})
const editable = computed(() => spec.value?.schema_version === 2 ? !stale.value && editableDirectorNode(selectedStage.value) : !!node.value && editableDirectorNode(node.value.stage) && summary.value?.mode === 'professional' && !!summary.value?.available_actions.includes('edit_stage'))
const visibleNodes = computed(() => mode.value === 'fast' ? fastDirectorNodeLabels : directorNodeLabels)
const projectTitle = computed(() => conversations.value.find(item => item.id === activeConversationId.value)?.title ?? '新对话')
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
  const epoch = conversationEpoch
  try {
    const saved = await confirmDirectorVersion(project.value.project.project_id, Number(spec.value.version), project.value.project.current_version)
    if (epoch !== conversationEpoch) return
    restoredSpec.value = saved; await refresh()
  }
  catch (failure) { if (epoch === conversationEpoch) error.value = failureText(failure) }
  finally { if (epoch === conversationEpoch) busy.value = false }
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
  const epoch = conversationEpoch
  const owner = activeConversationId.value
  busy.value = true; error.value = ''
  try {
    const saved = await saveDirectorDraft(project.value.project.project_id, {
      expected_project_version: project.value.project.current_version,
      expected_director_version: instruction ? revisionVersion.value : spec.value.version,
      conversation_id: owner,
      ...(instruction ? { revision_instruction: instruction } : { draft: editableDirectorDraft(spec.value, fields.value) }),
    })
    if (epoch !== conversationEpoch) return
    restoredSpec.value = saved
    delete drafts.value[key]; revisionVersion.value = null
    await refresh()
  } catch (failure) { if (epoch === conversationEpoch) error.value = failureText(failure) }
  finally { if (epoch === conversationEpoch) busy.value = false }
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
  const epoch = conversationEpoch
  const owner = activeConversationId.value
  try {
    const [context, tasks, nextAssets] = await Promise.all([getComicProject(id), getDirectorExecutions(id), getComicAssets(id)])
    if (disposed || epoch !== conversationEpoch || id !== project.value?.project.project_id) return
    if (context) project.value = context
    const records = await Promise.all(tasks.map(item => getRun(item.run_id)))
    if (disposed || epoch !== conversationEpoch || id !== project.value?.project.project_id) return
    records.forEach(run => { if (run) runs.value[run.id] = run })
    executions.value = tasks.filter(item => runs.value[item.run_id] && ownsConversationRun(runs.value[item.run_id]!, owner))
    assets.value = nextAssets
    await loadConversationMessages(owner)
    if (productionRun.value) {
      const previousStatus = productionRun.value.status
      const [run, updates] = await Promise.all([getRun(productionRun.value.id), getEvents(productionRun.value.id)])
      if (disposed || epoch !== conversationEpoch) return
      productionRun.value = run; productionEvents.value = updates
      if (run) runs.value[run.id] = run
      if (run && !productionAdvanced && section.value === 'director' && (['storyboard', 'prompt', 'prepare', 'generate', 'qc', 'archive'].includes(run.current_node) || (run.status === 'completed' && !!(run.state.quick_creation as Record<string, unknown>)?.storyboard_id))) {
        productionAdvanced = true
        section.value = 'storyboard'
        chatExpanded.value = false
        await loadPage()
      }
      if (run?.status === 'completed' && previousStatus !== 'completed') { await loadConversationMessages(owner); await loadPage() }
      if (run?.status === 'failed') error.value = `${run.error ?? '图片制作失败'} · ${String(run.state.error_id ?? '')}`
      if (run?.status === 'waiting') error.value = run.error ?? '图片任务等待查询或对账；可在当前工作区恢复，不会重新提交。'
    }
    if (inspectorOpen.value && active.value) {
      const loadedEvents = await getEvents(active.value.run_id)
      if (epoch === conversationEpoch) events.value = loadedEvents
    }
  } finally { if (epoch === conversationEpoch) refreshing = false }
}
async function execute(text: string, options: Record<string, unknown> = {}, selectedMode: CreationMode = mode.value): Promise<void> {
  if (busy.value || hasRunning.value || (!project.value && !text.trim())) { composer.value?.fill(text); return }
  const editingKey = draftKey.value
  const epoch = conversationEpoch
  const owner = activeConversationId.value
  const sourceProject = project.value
  if (!owner) { error.value = '请先打开或创建对话'; return }
  busy.value = true; pendingText.value = text; previousRunIds.value = executions.value.map(item => item.run_id); error.value = ''; invalidate()
  restoredSpec.value = null
  try {
    if (text.trim() && !manualDirectorApproval.value && !Object.keys(options).length) {
      if (!pendingCreation || pendingCreation.text !== text || pendingCreation.mode !== selectedMode) pendingCreation = { text, mode: selectedMode, requestId: `creation-${crypto.randomUUID()}` }
      const run = await createRun('comic', { creative_request: text, conversation_id: owner,
        creation_mode: selectedMode, approval_required: false, request_id: pendingCreation.requestId })
      if (disposed || epoch !== conversationEpoch || owner !== activeConversationId.value) return
      pendingCreation = null; productionRun.value = run; productionAdvanced = false; runs.value[run.id] = run
      const creation = run.state.quick_creation as Record<string, unknown>
      const context = await getComicProject(String(creation.project_id))
      if (disposed || epoch !== conversationEpoch) return
      project.value = context
      pendingText.value = ''; section.value = 'director'; selectedStage.value = 'director_assemble'
      await refresh(); await loadConversations()
      return
    }
    // Navigation detaches the view, not the already submitted request's ownership.
    const target = sourceProject
      ? await getComicProject(sourceProject.project.project_id) ?? sourceProject
      : await createComicProject(text)
    if (epoch === conversationEpoch && !disposed) project.value = target
    const conversation = owner
    const result = await createDirectorExecution(target.project.project_id, {
      expected_project_version: target.project.current_version, creation_mode: selectedMode,
      creative_operation: 'new',
      conversation_id: conversation,
      ...('previous_run_id' in options || 'resume_run_id' in options ? {} : { task: text || null }), ...options,
    })
    if (disposed || epoch !== conversationEpoch || owner !== activeConversationId.value) return
    selectedRun.value = result.run_id; restoredSpec.value = null
    if ('stage_edits' in options) delete drafts.value[editingKey]
    await refresh()
    if (epoch !== conversationEpoch || disposed) return
    pendingText.value = ''
    await loadConversations()
  } catch (failure) {
    if (epoch !== conversationEpoch || disposed) return
    error.value = failureText(failure)
    await refresh().catch(() => undefined)
    if (epoch !== conversationEpoch || disposed) return
    const failedRun = selectDirectorExecution(executions.value, '', true, previousRunIds.value)
    if (failedRun) { selectedRun.value = failedRun.run_id; pendingText.value = '' }
  } finally { if (epoch === conversationEpoch) busy.value = false }
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
/* Title belongs to the sending conversation, never to Project or the selected Run. */
async function titleConversation(text: string): Promise<void> {
  const id = activeConversationId.value
  const current = conversations.value.find(item => item.id === id)
  if (!id || !current || !/^新对话$/.test(current.title)) return
  const title = conversationTaskTitle(text)
  if (!title) return
  current.title = title
  await renameConversation(id, title).catch((failure) => { if (activeConversationId.value === id) error.value = failureText(failure) })
  await loadConversations()
}
async function dispatchQueued(explicit = false): Promise<void> {
  if (explicit) error.value = ''
  if (!canDispatch.value || disposed) return
  const next = queuedInputs.value.shift()
  if (next) await execute(next.text, {}, next.mode)
}
async function cancelExecution(): Promise<void> {
  const task = productionRun.value && ['running', 'pending'].includes(productionRun.value.status)
    ? { run_id: productionRun.value.id } : runningExecution.value
  if (!task || cancelling.value) return
  const epoch = conversationEpoch
  cancelling.value = true
  try { await cancelRun(task.run_id); if (epoch === conversationEpoch) await refresh() }
  catch (failure) { if (epoch === conversationEpoch) error.value = failureText(failure) }
  finally { if (epoch === conversationEpoch) cancelling.value = false }
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
  const epoch = conversationEpoch
  if (!id) { shots.value = []; return }
  try {
    const next = await getComicShots(id)
    if (id !== selectedBoard.value || disposed) return
    shots.value = next
    if (!next.some(item => item.shot_id === selectedShot.value)) selectedShot.value = next[0]?.shot_id ?? ''
    if (section.value === 'prompt') await loadPrompts()
  } catch (failure) { if (epoch === conversationEpoch) error.value = failureText(failure) }
}
async function loadPrompts(): Promise<void> {
  const id = selectedShot.value
  const epoch = conversationEpoch
  prompts.value = []
  if (!id) return
  try {
    const next = await getComicPromptVersions(id)
    if (id === selectedShot.value && !disposed) prompts.value = next
  } catch (failure) { if (epoch === conversationEpoch) error.value = failureText(failure) }
}
async function compilePrompt(): Promise<void> {
  const shot = shots.value.find(item => item.shot_id === selectedShot.value)
  const board = boards.value.find(item => item.storyboard_id === selectedBoard.value)
  if (!project.value || !shot || (manualDirectorApproval.value && !confirmed.value) || board?.director_spec_version !== spec.value?.version || compiling.value || busy.value || hasRunning.value) return
  const epoch = conversationEpoch
  compiling.value = true; error.value = ''
  try {
    await compileComicPrompt(shot.shot_id, project.value.project.current_version, shot.version)
    if (epoch !== conversationEpoch) return
    await refresh()
    if (epoch === conversationEpoch) await loadPrompts()
  } catch (failure) { if (epoch === conversationEpoch) error.value = failureText(failure) }
  finally { if (epoch === conversationEpoch) compiling.value = false }
}
async function generateImage(newGeneration = false): Promise<void> {
  if (!productionEligible.value || (manualDirectorApproval.value && !confirmed.value) || !project.value || !spec.value || submittingImage.value || compiling.value || busy.value || hasRunning.value) return
  const epoch = conversationEpoch
  const shot = ['storyboard', 'prompt'].includes(section.value) ? shots.value.find(item => item.shot_id === selectedShot.value) : undefined
  const promptVersion = section.value === 'prompt' && shot ? prompts.value[0]?.version : undefined
  const key = `${activeConversationId.value}:${project.value.project.project_id}:${spec.value.version}:${shot?.shot_id ?? 'keyframe'}:${shot?.version ?? ''}:${manualDirectorApproval.value}:${promptVersion ?? ''}`
  // A network retry keeps the same request identity; it must never submit a second image.
  if (newGeneration) productionRequests.delete(key)
  if (!productionRequests.has(key)) productionRequests.set(key, `production-${crypto.randomUUID()}`)
  submittingImage.value = true; error.value = ''
  try {
    const run = await createRun('comic', {
      production_project_id: project.value.project.project_id,
      expected_project_version: project.value.project.current_version,
      director_version: spec.value.version, conversation_id: activeConversationId.value,
      approval_required: manualDirectorApproval.value,
      request_id: productionRequests.get(key),
      ...(shot ? { shot_id: shot.shot_id, shot_version: shot.version } : {}),
      ...(promptVersion ? { prompt_version: promptVersion } : {}),
    })
    if (epoch !== conversationEpoch || disposed) return
    productionRun.value = run; runs.value[run.id] = run; productionEvents.value = []
    productionAdvanced = true
    openSection('storyboard')
    await refresh()
    if (epoch === conversationEpoch) await loadPage()
  } catch (failure) { if (epoch === conversationEpoch) error.value = failureText(failure) }
  finally { if (epoch === conversationEpoch) submittingImage.value = false }
}
async function recoverImage(): Promise<void> {
  if (!productionRun.value || busy.value || hasRunning.value) return
  const epoch = conversationEpoch
  busy.value = true; error.value = ''
  try { const run = await resumeRun(productionRun.value.id); if (epoch === conversationEpoch) { productionRun.value = run; await refresh() } }
  catch (failure) { if (epoch === conversationEpoch) error.value = failureText(failure) }
  finally { if (epoch === conversationEpoch) busy.value = false }
}
async function regenerateImage(): Promise<void> {
  if (!imageExecution.value?.can_regenerate || busy.value || hasRunning.value) return
  if (productionEligible.value) await generateImage(true)
  else {
    const request = String((productionRun.value?.state.quick_creation as Record<string, unknown>)?.original_request ?? '')
    if (request) await execute(request)
  }
}
function editImagePrompt(): void {
  if (!selectedShot.value || !prompts.value[0]) { openSection('prompt'); void loadPage(); return }
  const prompt = prompts.value[0]!
  promptEdit.value = { shotId: selectedShot.value, version: Number(prompt.version), director_summary: String(prompt.director_summary), positive_prompt: String(prompt.positive_prompt), negative_prompt: String(prompt.negative_prompt ?? '') }
  openSection('prompt')
}
async function saveImagePrompt(): Promise<void> {
  if (!promptEdit.value || !project.value || busy.value || hasRunning.value) return
  const epoch = conversationEpoch
  busy.value = true; error.value = ''
  const { shotId, version, ...draft } = promptEdit.value
  try {
    await saveComicPrompt(shotId, { expected_project_version: project.value.project.current_version, expected_version: version, draft })
    if (epoch !== conversationEpoch) return
    promptEdit.value = null; await refresh(); await loadPrompts()
  } catch (failure) { if (epoch === conversationEpoch) error.value = failureText(failure) }
  finally { if (epoch === conversationEpoch) busy.value = false }
}
function changeImageModel(): void {
  error.value = '模型从 config/settings.yaml 的 image.model / image.protocol 读取。修改兼容模型并重启后端后再生成；原任务结果未知时需先查询或对账，不能通过换模型重提。'
}
async function enterStoryboard(): Promise<void> {
  if (!confirmed.value || !productionEligible.value || busy.value || hasRunning.value) return
  openSection('storyboard')
  await generateImage()
}
async function restore(version: number): Promise<void> {
  if (!project.value || busy.value || hasRunning.value) return
  busy.value = true; invalidate()
  const epoch = conversationEpoch
  try {
    const restored = await restoreDirectorVersion(project.value.project.project_id, version, project.value.project.current_version)
    if (epoch !== conversationEpoch) return
    restoredSpec.value = restored
    await refresh()
    if (epoch !== conversationEpoch) return
    await loadPage()
    if (epoch === conversationEpoch) { section.value = 'director'; selectedStage.value = 'director_assemble' }
  } catch (failure) { if (epoch === conversationEpoch) error.value = failureText(failure) }
  finally { if (epoch === conversationEpoch) { busy.value = false; restoreChoice.value = null } }
}
function newProject(): void {
  if (busy.value || hasRunning.value || ((Object.keys(drafts.value).length || queuedInputs.value.length) && !window.confirm('放弃未保存的修改和待发送补充并新建作品？'))) return
  void startConversation()
}
function resetConversationView(): void {
  productionRun.value = null; productionEvents.value = []; pendingCreation = null; productionAdvanced = false
  submittingImage.value = false; mode.value = 'fast'; manualDirectorApproval.value = false; promptEdit.value = null
  refreshing = false
  busy.value = false; cancelling.value = false; compiling.value = false; legacyOnly.value = false
  queuedInputs.value = []; previousRunIds.value = []; events.value = []; restoreChoice.value = null
  conversationMessages.value = []; composer.value?.fill(''); scrollPositions.clear()
  pageRequest++; project.value = null; executions.value = []; runs.value = {}; assets.value = []; boards.value = []; shots.value = []; versions.value = []; prompts.value = []
  selectedRun.value = ''; restoredSpec.value = null; drafts.value = {}; revisionVersion.value = null; error.value = ''; pendingText.value = ''
  section.value = 'director'; selectedStage.value = 'director_assemble'; chatExpanded.value = true; inspectorOpen.value = false; restoreChoice.value = null
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
  if (!loading.value) manualDirectorApproval.value = value === 'professional'
  if (value === 'fast' && selectedStage.value === 'director_critic') selectedStage.value = 'director_assemble'
  if (project.value) sessionStorage.setItem(`kantoku-comic-view-mode:${project.value.project.project_id}`, value)
}, { flush: 'sync' })
watch([canDispatch, () => queuedInputs.value.length], () => { void dispatchQueued() })
watch(() => `${project.value?.project.project_id}:${mode.value}:${section.value}:${selectedStage.value}:${selectedRun.value}:${spec.value?.version}:${chatExpanded.value}`, async (key, old) => {
  if (stageScroll.value) scrollPositions.set(old, stageScroll.value.scrollTop)
  await nextTick()
  if (stageScroll.value) stageScroll.value.scrollTop = scrollPositions.get(key) ?? 0
})
watch(inspectorOpen, async open => {
  if (!open || !active.value) return
  const epoch = conversationEpoch
  try {
    const loaded = await getEvents(active.value.run_id)
    if (epoch === conversationEpoch) events.value = loaded
  } catch (failure) { if (epoch === conversationEpoch) error.value = failureText(failure) }
})
watch(() => transcript.value.map(item => `${item.execution.run_id}:${item.execution.status}`).join('|') + pendingText.value + queuedInputs.value.length + productionRun.value?.current_node + productionRun.value?.status + conversationMessages.value.length, async () => { if (!nearBottom.value) return; await nextTick(); timeline.value?.scrollTo({ top: timeline.value.scrollHeight, behavior: 'auto' }) })
onMounted(async () => {
  try {
    await loadConversations()
    const legacy = props.initialRunId ? await getRun(props.initialRunId) : null
    const requestedPage = route.value.workspacePage
    const requestedStage = route.value.directorStage
    const requestedMode = route.value.creationMode
    const stored = localStorage.getItem('kantoku-comic-active-conversation')
    const id = route.value.conversationId ?? (typeof legacy?.state.conversation_id === 'string' ? legacy.state.conversation_id : null)
      ?? (conversations.value.some(item => item.id === stored) ? stored : null) ?? conversations.value[0]?.id
    if (id) await openConversation(id)
    else await startConversation()
    if (requestedMode) mode.value = requestedMode
    if (requestedPage && requestedPage !== 'conversation') {
      section.value = requestedPage; selectedStage.value = requestedStage ?? 'director_assemble'; chatExpanded.value = false
      await loadPage()
    }
  } catch (failure) { error.value = failureText(failure) }
  finally { loading.value = false }
  timer = setInterval(() => {
    const epoch = conversationEpoch
    void refresh().catch(failure => { if (epoch === conversationEpoch) error.value = failureText(failure) })
  }, 2000)
})
onBeforeUnmount(() => { disposed = true; pageRequest++; if (timer) clearInterval(timer); Object.values(referenceUrls.value).forEach(url => URL.revokeObjectURL(url)) })
</script>

<template>
  <WorkspaceShell v-model:navigation-open="navOpen" :hide-heading="chatExpanded" :navigation="navigation" :active-page="activeNavigation" :creating-conversation="conversationBusy" @select-page="openSection" @new-conversation="startConversation">
    <template #toolbar>
      <strong class="toolbar-project">{{ projectTitle }}</strong>
      <label class="toolbar-mode"><span>创作模式</span>
        <select v-model="mode" :disabled="busy || hasRunning || loading">
          <option value="fast">普通模式</option><option value="professional">专业导演模式</option>
        </select>
      </label>
      <label v-if="mode === 'professional'" class="toolbar-mode"><input v-model="manualDirectorApproval" type="checkbox" :disabled="busy || hasRunning"> 人工审核模式</label>
      <span class="toolbar-status" role="status">{{ executionLabel }}</span>
      <button class="ui-button quiet sm toolbar-new" :disabled="busy || hasRunning" @click="newProject">新作品</button>
    </template>
    <template #recent><ConversationHistory list-only :conversations="conversations" :active-id="activeConversationId" :busy="conversationBusy" @select="openConversation" @rename="finishConversationRename" @remove="removeConversation" /></template>
            <template #heading>
              <div><small>{{ chatExpanded ? 'AI 导演助手' : section === 'director' ? '导演工作区' : '项目工作区' }}</small><h2>{{ chatExpanded ? projectTitle : title }}</h2></div>
              <span v-if="!chatExpanded && section === 'director' && spec" class="node-version">v{{ spec.version }}<span v-if="dirty"> · 未保存修改</span></span>
              <button v-if="!chatExpanded" class="ui-button quiet sm" :aria-expanded="inspectorOpen" @click="inspectorOpen = !inspectorOpen"><PanelRight :size="15" /> 详情</button>
            </template>
        <div class="workspace-content">
          <main class="workspace-stage">

              <div v-if="!chatExpanded" :key="`${section}:${selectedStage}:${selectedRun}`" ref="stageScroll" class="stage-scroll" aria-label="当前节点工作区">
                <ImageGenerationPlaceholder v-if="productionProgress?.visible" :label="productionProgress.label" :percent="productionProgress.percent" :started-at="productionRun?.started_at" />
                <ChatImageAttachment v-if="section !== 'director' && productionRun?.state.image_artifact_id && referenceUrls[String(productionRun.state.image_artifact_id)]" :media="{ url: referenceUrls[String(productionRun.state.image_artifact_id)]!, filename: `comic-${productionRun.state.image_artifact_id}.png` }" @open="previewReference" />
                <template v-if="section === 'director' && !productionProgress?.active">
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
                  <div v-if="manualDirectorApproval && spec && selectedStage === 'director_assemble'" class="final-approval"><p>{{ stale ? '来源已变化，需要更新方案' : confirmed ? '当前版本已确认，可以进入分镜制作' : dirty ? '请先保存编辑，再检查并确认' : '这是可修改的草稿，确认后才进入下一阶段。' }}</p><div class="draft-actions"><button class="ui-button sm" :disabled="busy || hasRunning || stale || dirty" @click="reviewFinal">检查当前方案</button><button class="ui-button quiet sm" :disabled="busy || hasRunning || stale || dirty" @click="reviseByInstruction">用对话修改</button><button class="ui-button quiet sm" :disabled="busy || hasRunning || dirty" @click="regenerate">重新生成</button><button class="ui-button primary sm" :disabled="!confirmable || confirmed" @click="confirm">确认最终方案</button><button class="ui-button sm" :disabled="!confirmed || !productionEligible || submittingImage || busy || hasRunning" @click="enterStoryboard">进入下一步</button></div></div>
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
                  <div v-if="section === 'prompt' && promptEdit" class="draft-actions">
                    <label>正向 Prompt<textarea v-model="promptEdit.positive_prompt" rows="6" /></label>
                    <label>负向约束<textarea v-model="promptEdit.negative_prompt" rows="3" /></label>
                    <button class="ui-button primary sm" :disabled="busy || hasRunning || !promptEdit.positive_prompt.trim()" @click="saveImagePrompt">保存为新版本</button>
                    <button class="ui-button quiet sm" @click="promptEdit = null">取消修改</button>
                  </div>
                  <button v-if="section === 'prompt' && prompts.length && !promptEdit" class="ui-button sm" :disabled="busy || hasRunning" @click="editImagePrompt">编辑当前 Prompt</button>
                  <p v-if="manualDirectorApproval && !confirmed" class="review-notice">请先确认当前导演方案再制作。</p>
                  <button v-if="section === 'storyboard' && productionEligible" class="ui-button primary sm" :disabled="submittingImage || compiling || busy || hasRunning || (manualDirectorApproval && !confirmed)" @click="generateImage()">{{ submittingImage ? '正在创建分镜制作任务' : selectedShot ? '生成当前镜头图片' : '生成分镜并制作图片' }}</button>
                  <label v-if="boards.length">分镜 <select v-model="selectedBoard" @change="loadShots"><option v-for="board in boards" :key="board.storyboard_id" :value="board.storyboard_id">{{ board.title }} · v{{ board.version }}</option></select></label>
                  <p v-else class="pane-note">{{ productionProgress?.active ? '正在规划分镜，将继续编译提示词并生成图片。' : '尚无作品分镜。确认导演方案后，可开始分镜制作。' }}</p>
                  <template v-if="section === 'storyboard'"><article v-for="shot in shots" :key="shot.shot_id" class="shot-row"><strong>镜头 {{ shot.sequence_number }} · {{ shot.subject }}</strong><p>{{ shot.purpose }} · {{ shot.action }}</p><small>v{{ shot.version }} · {{ directorStateLabels[shot.status] ?? shot.status }} · 角色 {{ shot.character_asset_versions.map(ref => `${ref.asset_id} v${ref.version}`).join('、') || '未引用' }}</small></article></template>
                  <template v-else><label v-if="shots.length">镜头 <select v-model="selectedShot" @change="loadPrompts"><option v-for="shot in shots" :key="shot.shot_id" :value="shot.shot_id">{{ shot.sequence_number }} · {{ shot.subject }}</option></select></label><details v-for="prompt in prompts" :key="String(prompt.prompt_id) + prompt.version"><summary>Prompt v{{ prompt.version }} · {{ prompt.model_target }}</summary><p>{{ prompt.positive_prompt }}</p><h3>负向约束</h3><p>{{ prompt.negative_prompt }}</p><small>导演 v{{ prompt.director_spec_version }} · 镜头 v{{ prompt.shot_version }} · {{ prompt.compiler_version }}</small></details><p v-if="selectedShot && !prompts.length" class="pane-note">当前镜头没有已保存的 Prompt 版本。</p><button v-if="selectedShot" class="ui-button primary sm" :disabled="(manualDirectorApproval && !confirmed) || compiling || busy || hasRunning || boards.find(board => board.storyboard_id === selectedBoard)?.director_spec_version !== spec?.version" @click="compilePrompt">{{ compiling ? '正在编译 Prompt' : '编译当前镜头 Prompt' }}</button><p v-if="selectedShot && boards.find(board => board.storyboard_id === selectedBoard)?.director_spec_version !== spec?.version" class="pane-note">分镜引用的导演版本与当前方案不同。请先更新分镜，旧 Prompt 可继续查看。</p><button class="ui-button sm" @click="section = 'assets'">查看生成结果</button></template>
                </template>
                <template v-else-if="section === 'history'">
                  <h3>方案版本</h3><article v-for="version in versions" :key="Number(version.version)" class="history-row"><header><div><strong>导演方案 v{{ version.version }}</strong><small>{{ version.created_at }}</small></div><button class="ui-button sm" :disabled="busy || hasRunning" @click="restoreChoice = Number(version.version)">恢复为新版本</button></header><details><summary>查看版本摘要</summary><AssistantMessageBlock :content="directorSummary(version, mode) || '旧版方案（只读）'" :show-mark="false" /></details><div v-if="restoreChoice === Number(version.version)" class="review-notice" role="status"><p>将 v{{ version.version }} 恢复为新版本。历史保留，当前确认状态会清除。</p><button class="ui-button primary sm" :disabled="busy || hasRunning" @click="restore(Number(version.version))">确认恢复</button><button class="ui-button quiet sm" :disabled="busy" @click="restoreChoice = null">取消</button></div></article><p v-if="!versions.length" class="pane-note">暂无已保存方案版本。</p>
                  <h3>真实执行记录</h3><button v-for="item in executions" :key="item.run_id" class="history-task" @click="selectedRun = item.run_id; restoredSpec = null; visitStage('director_assemble')">{{ item.director_execution_summary.mode === 'fast' ? '普通模式' : '专业模式' }} · {{ directorStateLabels[item.status] ?? item.status }}<small>{{ item.run_id }}</small></button>
                </template>
              </div>
            <section v-show="chatExpanded" ref="timeline" class="workspace-messages" aria-label="连续创作对话" @scroll="scrollState">
              <p v-if="!chatTurns.length && !pendingText" class="conversation-welcome">描述你想创作的画面，AI 会自动理解并制作图片。需要逐节点修改与确认时，可主动选择专业导演模式。</p>
              <article v-for="turn in chatTurns" :key="turn.id" class="creative-turn">
                <UserMessageBubble v-if="turn.role === 'user'" :content="turn.content" />
                <template v-else><AssistantMessageBlock :content="turn.content" :show-mark="false" /><ChatImageAttachment v-if="turn.artifactId && referenceUrls[turn.artifactId]" :media="{ url: referenceUrls[turn.artifactId]!, filename: `comic-${turn.artifactId}.png` }" @open="previewReference" /><p v-if="turn.artifactId && referenceErrors[turn.artifactId]" role="alert">{{ referenceErrors[turn.artifactId] }}</p></template>
              </article>
              <div v-if="manualDirectorApproval && !restoredSpec && active?.director_spec" class="draft-actions"><button class="ui-button sm" @click="openDraft(active.run_id)">进入导演方案 <ArrowRight :size="15" /></button><span>{{ stale ? '来源已变化' : confirmed ? '已确认' : '待确认草稿' }}</span></div>
              <UserMessageBubble v-if="pendingText && !transcript.some(entry => !previousRunIds.includes(entry.execution.run_id) && entry.text === pendingText)" :content="pendingText" :pending="busy ? 'replying' : 'failed'" />
              <p v-if="busy && !active" role="status">正在提交创意，等待真实执行状态…</p>
              <ImageGenerationPlaceholder v-if="productionProgress?.visible" :label="productionProgress.label" :percent="productionProgress.percent" :started-at="productionRun?.started_at" />
              <p v-if="active?.status === 'cancelled'" class="pane-note" role="status">任务已取消。正在进行的模型请求可能仍需结束，但不会继续下一节点或保存导演方案。</p>
              <article v-for="input in queuedInputs" :key="input.id" class="queued-turn">
                <UserMessageBubble :content="input.text" />
                <div class="queued-note"><span>补充已排队 · 当前方案结束后处理 · {{ input.mode === 'professional' ? '专业模式' : '普通模式' }}</span><button class="ui-button quiet sm" @click="queuedInputs = queuedInputs.filter(item => item.id !== input.id)">撤回</button></div>
              </article>
              <button v-if="queuedInputs.length && error && !busy && !hasRunning" class="ui-button sm" @click="dispatchQueued(true)">继续处理已发送的补充</button>
              <p v-if="error" class="workspace-error" role="alert">{{ error }}</p>
              <p v-if="dirty" class="pane-note">有未保存的节点修改。切换节点会保留草稿；保存并重新审核后才能确认。</p>
              <template v-if="manualDirectorApproval && restoredSpec"><AssistantMessageBlock :content="directorConversationSummary(restoredSpec)" :show-mark="false" /><div class="draft-actions"><span>方案 v{{ restoredSpec.version }} · {{ confirmed ? '已确认' : '草稿' }}</span><button class="ui-button sm" @click="openDraft()">进入导演方案 <ArrowRight :size="15" /></button></div></template>
              <button v-if="manualDirectorApproval && spec && !confirmable && !stale && !dirty" class="ui-button sm" :disabled="busy || hasRunning" @click="reviewFinal">审核当前草稿</button>
              <p v-if="manualDirectorApproval && confirmed" class="pane-note">当前版本已确认并保存到后端。编辑或恢复为新版本后，需要重新审核与确认。</p>
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
          <div class="composer-context"><button class="ui-button quiet sm" :aria-expanded="chatExpanded" @click="chatExpanded = !chatExpanded"><ArrowLeft v-if="chatExpanded" :size="15" /><MessageSquareText v-else :size="15" />{{ chatExpanded ? '返回工作区' : '打开创作对话' }}</button><small>{{ revisionVersion !== null ? `修改当前草稿 v${revisionVersion}` : '当前对话' }}</small><button v-if="revisionVersion !== null" class="ui-button quiet sm" @click="revisionVersion = null">取消修改</button><button v-else-if="spec" class="ui-button quiet sm" :disabled="busy || hasRunning || stale || dirty" @click="reviseByInstruction">修改当前方案</button></div>
          <p v-if="legacyOnly" class="pane-note">此历史任务没有作品级 Project；点击"新作品"进入作品级创作。</p>
          <div v-if="busy || hasRunning" class="execution-controls" role="status"><span>{{ executionLabel }} · 可继续输入</span><button v-if="hasRunning" class="ui-button quiet sm" :disabled="cancelling" @click="cancelExecution">{{ cancelling ? '正在取消' : '取消当前任务' }}</button></div>
          <p v-if="!chatExpanded && error" class="workspace-error" role="alert">{{ error }}</p>
          <div v-if="productionRun?.status === 'failed' || productionRun?.status === 'waiting'" class="draft-actions">
            <button v-if="imageExecution?.can_resume" class="ui-button sm" :disabled="busy || hasRunning" @click="recoverImage">继续查询原任务</button>
            <button v-if="productionRun.status === 'failed'" class="ui-button sm" :disabled="!imageExecution?.can_regenerate || busy || hasRunning" @click="regenerateImage">重新生成</button>
            <button class="ui-button quiet sm" :disabled="busy || hasRunning" @click="editImagePrompt">修改 Prompt</button>
            <button class="ui-button quiet sm" @click="changeImageModel">更换模型</button>
            <span v-if="imageExecution?.needs_reconciliation">原请求账单未知，需先对账；不会自动重提。</span>
          </div>
          <p v-if="productionRun?.status === 'completed' && imageExecution" class="pane-note">模型：{{ imageExecution.model }}<template v-if="imageElapsed !== null"> · 生图耗时：{{ imageElapsed }}s</template> · 图片成本：{{ imageExecution.actual_fen === null ? '待结算（供应商未返回账单）' : `¥${(imageExecution.actual_fen / 100).toFixed(2)}` }}</p>
          <MessageComposer :key="activeConversationId" ref="composer" :disabled="loading || legacyOnly" @send="sendInput" />
          <small>{{ revisionVersion !== null ? '发送将保存当前方案的新修订；不会创建新创意。' : queuedInputs.length ? '补充已排队，刷新会丢失未执行补充；可在对话中撤回。' : manualDirectorApproval ? '方案先保存为草稿，审核并确认后再进入制作。' : '自动完成导演、分镜、提示词与图片制作。' }}</small>
        </div>
</template>
  </WorkspaceShell>
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
