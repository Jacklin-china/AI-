<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Clapperboard, Layers, History, FileText, Film, MessageSquareText, PanelLeft, PanelRight, ChevronUp, ChevronDown, X, Check } from 'lucide-vue-next'
import { Pane, Splitpanes } from 'splitpanes'
import 'splitpanes/dist/splitpanes.css'
import MessageComposer from './chat/MessageComposer.vue'
import UserMessageBubble from './chat/UserMessageBubble.vue'
import AssistantMessageBlock from './chat/AssistantMessageBlock.vue'
import DirectorNodeView from './DirectorNodeView.vue'
import ChatImageAttachment from './chat/ChatImageAttachment.vue'
import { canConfirmDirector, chronologicalDirectorExecutions, directorFieldLabels, directorIsStale, directorNodeLabels, directorStateLabels, directorSummary, draftConfirmationKey, editableDirectorNode, selectDirectorExecution, stageDraftKey } from '../domains/comic/directorPresentation'
import { CoreApiError, compileComicPrompt, createComicProject, createConversation, createDirectorExecution, getArtifactContentUrl, getComicAssets, getComicProject, getComicPromptVersions, getComicShots, getComicStoryboards, getCurrentDirector, getDirectorExecutions, getDirectorVersions, getEvents, getRun, restoreDirectorVersion, type ComicAssetView, type ComicProjectContext, type ComicShotView, type ComicStoryboardView, type CreationMode, type DirectorExecution, type RuntimeEvent } from '../services/core'
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
const selectedStage = ref('director_assemble')
const section = ref('director')
const navOpen = ref(false)
const inspectorOpen = ref(false)
const chatExpanded = ref(false)
const chatSize = ref(58)
const composer = ref<InstanceType<typeof MessageComposer> | null>(null)
const timeline = ref<HTMLElement | null>(null)
const nearBottom = ref(true)
const busy = ref(false)
const loading = ref(true)
const error = ref('')
const pendingText = ref('')
const previousRunIds = ref<string[]>([])
const drafts = ref<Record<string, Record<string, string>>>({})
const confirmedKey = ref('')
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
let pageRequest = 0
const navigation = [
  { id: 'conversation', label: '对话', icon: MessageSquareText },
  { id: 'director', label: '导演', icon: Clapperboard },
  { id: 'storyboard', label: '分镜', icon: Film },
  { id: 'prompt', label: 'Prompt', icon: FileText },
  { id: 'assets', label: '资产', icon: Layers },
  { id: 'history', label: '历史', icon: History },
]
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
const draftKey = computed(() => stageDraftKey(project.value?.project.project_id ?? '', active.value?.run_id ?? '', selectedStage.value))
const fields = computed(() => drafts.value[draftKey.value] ?? {})
const editing = computed(() => draftKey.value in drafts.value)
const dirty = computed(() => Object.keys(drafts.value).some(key => JSON.parse(key)[1] === active.value?.run_id))
const stale = computed(() => directorIsStale(spec.value, project.value?.creative_brief.version, project.value?.project.director_version, assets.value))
const confirmationKey = computed(() => draftConfirmationKey(project.value?.project.project_id ?? '', spec.value))
const confirmable = computed(() => canConfirmDirector(restoredSpec.value ? 'completed' : active.value?.status, spec.value, stale.value, dirty.value) && !busy.value && !hasRunning.value)
const confirmed = computed(() => confirmable.value && !!confirmationKey.value && confirmedKey.value === confirmationKey.value)
const editable = computed(() => !!node.value && editableDirectorNode(node.value.stage) && summary.value?.mode === 'professional' && !!summary.value?.available_actions.includes('edit_stage'))
const title = computed(() => section.value === 'director' ? directorNodeLabels[selectedStage.value] : navigation.find(item => item.id === section.value)?.label)
const activeNavigation = computed(() => chatExpanded.value ? 'conversation' : section.value)
const activeRun = computed(() => !restoredSpec.value && active.value ? runs.value[active.value.run_id] : null)
const transcript = computed(() => chronologicalDirectorExecutions(executions.value, runs.value).map(item => {
  const run = runs.value[item.run_id]
  const text = String(run?.state.task ?? '')
  const rerun = String(run?.state.rerun_from ?? '').replace('comic.', '')
  return { execution: item, text: rerun ? `修改 / 重新执行：${directorNodeLabels[rerun] ?? rerun}` : text,
    answer: directorSummary(item.director_spec), label: item.director_execution_summary.status_label }
}))

function failureText(failure: unknown): string {
  if (failure instanceof CoreApiError) return `${failure.message}${failure.traceId ? ` · Trace：${failure.traceId}` : ''}`
  return failure instanceof Error ? failure.message : '操作未完成，请查看真实任务状态'
}
function rememberConfirmation(): void {
  if (project.value) localStorage.setItem(`kantoku-director-confirmation:${project.value.project.project_id}`, confirmedKey.value)
}
function confirm(): void { if (!confirmable.value) return; confirmedKey.value = confirmationKey.value; rememberConfirmation() }
function invalidate(): void { confirmedKey.value = ''; rememberConfirmation() }
function beginEdit(): void {
  if (!editable.value || !node.value) return
  if (!editing.value) drafts.value[draftKey.value] = Object.fromEntries(Object.entries(Object.values(node.value.output)[0] ?? {})
    .filter(([key, value]) => key !== 'hard_constraints' && key in directorFieldLabels && (typeof value === 'string' || Array.isArray(value)))
    .map(([key, value]) => [key, Array.isArray(value) ? value.join('\n') : String(value)]))
  invalidate()
}
function updateField(key: string, value: string): void { if (editing.value) drafts.value[draftKey.value]![key] = value }
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
    if (!busy.value && !hasRunning.value && !['failed', 'waiting'].includes(active.value?.status ?? '') && latestVersion && !tasks.some(item => item.director_spec?.version === latestVersion) && restoredSpec.value?.version !== latestVersion) {
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
async function execute(text: string, options: Record<string, unknown> = {}): Promise<void> {
  if (busy.value || hasRunning.value || (!project.value && !text.trim())) { composer.value?.fill(text); return }
  const selectedMode = mode.value
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
async function rerun(save = false): Promise<void> {
  if (!active.value || !node.value || !summary.value) return
  mode.value = summary.value.mode
  const options: Record<string, unknown> = { previous_run_id: active.value.run_id, rerun_from: node.value.stage }
  if (save) {
    const patch = { ...Object.values(node.value.output)[0] }
    Object.entries(fields.value).forEach(([key, text]) => { patch[key] = Array.isArray(patch[key]) ? text.split('\n').map(item => item.trim()).filter(Boolean) : text })
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
  if (busy.value || hasRunning.value || (Object.keys(drafts.value).length && !window.confirm('放弃未保存的节点修改并新建作品？'))) return
  pageRequest++; project.value = null; executions.value = []; runs.value = {}; assets.value = []; boards.value = []; shots.value = []; versions.value = []; prompts.value = []
  selectedRun.value = ''; restoredSpec.value = null; drafts.value = {}; conversationId.value = ''; confirmedKey.value = ''; error.value = ''; pendingText.value = ''
  section.value = 'director'; selectedStage.value = 'director_assemble'; inspectorOpen.value = false; restoreChoice.value = null
  localStorage.removeItem('kantoku-comic-project')
  if (props.initialRunId) emit('newProject')
}
function visitStage(stage: string): void { section.value = 'director'; selectedStage.value = stage }
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
watch(() => `${section.value}:${selectedStage.value}:${selectedRun.value}`, async (key, old) => {
  if (stageScroll.value) scrollPositions.set(old, stageScroll.value.scrollTop)
  await nextTick()
  if (stageScroll.value) stageScroll.value.scrollTop = scrollPositions.get(key) ?? 0
})
watch(inspectorOpen, async open => { if (open && active.value) { try { events.value = await getEvents(active.value.run_id) } catch (failure) { error.value = failureText(failure) } } })
watch(() => transcript.value.map(item => `${item.execution.run_id}:${item.execution.status}`).join('|') + pendingText.value, async () => { if (!nearBottom.value) return; await nextTick(); timeline.value?.scrollTo({ top: timeline.value.scrollHeight, behavior: 'auto' }) })
onMounted(async () => {
  try {
    const legacy = props.initialRunId ? await getRun(props.initialRunId) : null
    legacyOnly.value = !!legacy && !legacy.state.project_id
    const id = String(legacy ? legacy.state.project_id ?? '' : localStorage.getItem('kantoku-comic-project') ?? '')
    if (id) {
      project.value = await getComicProject(id); await refresh()
      if (project.value) localStorage.setItem('kantoku-comic-project', id)
      selectedRun.value = legacy?.workflow === 'comic.director' ? legacy.id : ''
      mode.value = active.value?.director_execution_summary.mode ?? 'fast'
      confirmedKey.value = localStorage.getItem(`kantoku-director-confirmation:${id}`) ?? ''
    }
    if (legacy && legacy.workflow !== 'comic.director') section.value = 'assets'
  } catch (failure) { error.value = failureText(failure) }
  finally { loading.value = false }
  timer = setInterval(() => { void refresh().catch(failure => { error.value = failureText(failure) }) }, 2000)
})
onBeforeUnmount(() => { disposed = true; pageRequest++; if (timer) clearInterval(timer); Object.values(referenceUrls.value).forEach(url => URL.revokeObjectURL(url)) })
</script>

<template>
  <section class="director-workspace" aria-label="AI 导演工作台">
    <header class="workspace-toolbar">
      <strong>{{ project?.project.title ?? (legacyOnly ? '历史制作记录' : '漫剧创作') }}</strong>
      <label>创作模式 <select v-model="mode" :disabled="busy || hasRunning || loading"><option value="fast">普通模式</option><option value="professional">专业导演模式</option></select></label>
      <span class="workspace-execution" role="status">{{ loading ? '载入中' : busy && !active ? '提交创意' : restoredSpec ? '已载入历史方案' : summary?.status_label ?? '等待创意' }}</span>
      <button class="ui-button quiet sm" :disabled="busy || hasRunning" @click="newProject">新作品</button>
    </header>
    <div class="workspace-body">
      <nav class="workspace-navigation" :class="{ expanded: navOpen }" aria-label="创作导航">
        <button :aria-expanded="navOpen" aria-label="展开项目导航" title="展开 / 收起导航" @click="navOpen = !navOpen"><PanelLeft :size="18" /></button>
        <button v-for="item in navigation" :key="item.id" :title="item.id === 'prompt' && !confirmed ? '请先确认导演方案' : item.label" :aria-label="item.label" :disabled="item.id === 'prompt' && !confirmed" :aria-current="activeNavigation === item.id ? 'page' : undefined" @click="openSection(item.id)"><component :is="item.icon" :size="18" /><span v-if="navOpen">{{ item.label }}</span></button>
      </nav>
      <Splitpanes horizontal class="workspace-split" @resized="payload => { if (payload.event) chatSize = payload.panes.at(-1)?.size ?? chatSize }">
        <Pane v-if="!chatExpanded && (mode === 'professional' || section !== 'director')" :size="100 - chatSize" :min-size="20">
          <div class="workspace-upper">
            <main class="workspace-stage">
              <header class="stage-heading"><h2>{{ title }}</h2><button class="ui-button quiet sm" :aria-expanded="inspectorOpen" @click="inspectorOpen = !inspectorOpen"><PanelRight :size="15" /> 详情</button></header>
              <nav v-if="mode === 'professional' && section === 'director'" class="stage-navigation" aria-label="导演节点">
                <button v-for="(label, stage) in directorNodeLabels" :key="stage" :aria-current="selectedStage === stage ? 'step' : undefined" @click="visitStage(String(stage))">{{ label }}<small>{{ restoredSpec ? '历史方案读取' : summary?.mode === 'fast' ? '普通模式未公开' : directorStateLabels[summary?.stages.find(item => item.stage === stage)?.status ?? 'pending'] ?? '未开始' }}</small></button>
              </nav>
              <div ref="stageScroll" class="stage-scroll">
                <template v-if="section === 'director'">
                  <p v-if="restoredSpec" class="pane-note">此方案恢复自历史版本，节点没有重新执行。继续修改可在下方对话中提出新方向；不会沿用其他版本的节点或 Trace。</p>
                  <p v-if="summary?.mode === 'fast' && selectedStage !== 'director_assemble'" class="pane-note">此方案在普通模式执行，后端未公开独立节点结果。切换模式不会伪造节点；可在专业模式发送新需求。</p>
                  <DirectorNodeView :stage="selectedStage" :node="node" :spec="spec" :fields="fields" :editing="editing" :editable="editable" :rerunnable="!!node && !!summary?.available_actions.includes('rerun_stage')" :busy="busy || hasRunning" :critic="summary?.critic_result" @edit="beginEdit" @field="updateField" @cancel="delete drafts[draftKey]" @save="rerun(true)" @rerun="rerun()" @revise="visitStage('visual_direction')" />
                  <div v-if="spec && selectedStage === 'director_assemble'" class="draft-actions"><strong>{{ stale ? '来源已变化，需要更新方案' : confirmed ? '本界面已确认' : '导演草案 · 待确认' }}</strong><button class="ui-button primary sm" :disabled="!confirmable || confirmed" @click="confirm">确认方案</button><button class="ui-button sm" :disabled="!confirmed" @click="section = 'prompt'">进入 Prompt</button></div>
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
                  <h3>方案版本</h3><article v-for="version in versions" :key="Number(version.version)" class="history-row"><strong>导演方案 v{{ version.version }}</strong><small>{{ version.created_at }}</small><AssistantMessageBlock :content="directorSummary(version) || '旧版方案（只读）'" :show-mark="false" /><button class="ui-button sm" :disabled="busy || hasRunning" @click="restoreChoice = Number(version.version)">恢复为新版本</button><div v-if="restoreChoice === Number(version.version)" class="review-notice" role="status"><p>将 v{{ version.version }} 恢复为新版本。历史保留，当前确认状态会清除。</p><button class="ui-button primary sm" :disabled="busy || hasRunning" @click="restore(Number(version.version))">确认恢复</button><button class="ui-button quiet sm" :disabled="busy" @click="restoreChoice = null">取消</button></div></article><p v-if="!versions.length" class="pane-note">暂无已保存方案版本。</p>
                  <h3>真实执行记录</h3><button v-for="item in executions" :key="item.run_id" class="history-task" @click="selectedRun = item.run_id; restoredSpec = null; visitStage('director_assemble')">{{ item.director_execution_summary.mode === 'fast' ? '普通模式' : '专业模式' }} · {{ directorStateLabels[item.status] ?? item.status }}<small>{{ item.run_id }}</small></button>
                </template>
              </div>
            </main>
            <aside v-if="inspectorOpen" class="workspace-inspector" aria-label="节点详情">
              <header><strong>详情</strong><button class="ui-button quiet sm" aria-label="关闭详情" @click="inspectorOpen = false"><X :size="15" /></button></header>
              <dl><div><dt>方案版本</dt><dd>{{ spec?.version ?? '未生成' }}</dd></div><div><dt>Brief</dt><dd>{{ spec?.creative_brief_version ?? '未关联' }}</dd></div><div v-for="(version, key) in node?.input_versions ?? activeRun?.state.input_versions ?? spec?.asset_versions ?? {}" :key="String(key)"><dt>{{ key }}</dt><dd>v{{ version }}</dd></div><div><dt>Run</dt><dd>{{ activeRun?.id ?? '本版本未关联' }}</dd></div><div><dt>Trace</dt><dd>{{ activeRun?.state.trace_id ?? '本版本未关联' }}</dd></div><div><dt>错误</dt><dd>{{ activeRun?.error ?? summary?.error_id ?? '无' }}</dd></div></dl>
              <details v-if="!restoredSpec"><summary>公开执行事件 · {{ events.length }}</summary><p v-for="event in events" :key="event.id">{{ event.event_type }}</p></details>
              <p class="pane-note">只展示真实版本、公开结果和执行事件，不展示模型私有思考。</p>
            </aside>
          </div>
        </Pane>
        <Pane key="conversation" :size="chatExpanded || mode === 'fast' && section === 'director' ? 100 : chatSize" :min-size="30">
          <section class="workspace-conversation" aria-label="连续创作对话">
            <header class="conversation-toolbar"><strong>AI 导演助手</strong><small v-if="conversationId">当前项目会话</small><span /><button class="ui-button quiet sm" :aria-expanded="chatExpanded" @click="chatExpanded = !chatExpanded"><ChevronDown v-if="chatExpanded" :size="15" /><ChevronUp v-else :size="15" />{{ chatExpanded ? '返回工作区' : '展开对话' }}</button></header>
            <div ref="timeline" class="workspace-messages" @scroll="scrollState">
              <p v-if="!transcript.length && !pendingText" class="conversation-welcome">描述你的故事、人物或希望观众感受到的情绪。我们从创作理解开始，再一起确认导演方案。</p>
              <article v-for="entry in transcript" :key="entry.execution.run_id" class="creative-turn">
                <UserMessageBubble v-if="entry.text" :content="entry.text" />
                <AssistantMessageBlock :content="entry.answer || entry.label" :show-mark="false" />
                <p v-if="entry.execution.status === 'failed'" role="alert">任务失败 · {{ entry.execution.director_execution_summary.error_id ?? '打开详情查看错误' }}</p>
                <template v-if="!restoredSpec && entry.execution.run_id === active?.run_id">
                  <div v-if="entry.execution.director_spec" class="draft-actions"><span><Check v-if="confirmed" :size="14" />{{ stale ? '来源已变化' : confirmed ? '本界面已确认' : '待确认的导演草案' }}</span><button class="ui-button quiet sm" :disabled="busy || hasRunning" @click="mode === 'professional' ? visitStage('visual_direction') : composer?.fill('请调整当前导演方案：')">修改方案</button><button class="ui-button quiet sm" :disabled="busy || hasRunning || dirty" @click="regenerate">重新生成方案</button><button class="ui-button primary sm" :disabled="!confirmable || confirmed" @click="confirm">确认方案</button><button class="ui-button quiet sm" :disabled="!confirmed" @click="section = 'prompt'; chatExpanded = false">进入 Prompt</button></div>
                  <button v-if="entry.execution.recovery_required || summary?.available_actions.includes('resume')" class="ui-button sm" :disabled="busy || hasRunning" @click="resume">恢复原导演任务</button>
                </template>
              </article>
              <UserMessageBubble v-if="pendingText && !transcript.some(entry => !previousRunIds.includes(entry.execution.run_id) && entry.text === pendingText)" :content="pendingText" :pending="busy ? 'replying' : 'failed'" />
              <p v-if="busy && !active" role="status">正在提交创意，等待真实执行状态…</p>
              <p v-if="error" class="workspace-error" role="alert">{{ error }}</p>
              <p v-if="dirty" class="pane-note">有未保存的节点修改。切换节点会保留草稿；保存并重新审核后才能确认。</p>
              <template v-if="restoredSpec"><AssistantMessageBlock :content="directorSummary(restoredSpec)" :show-mark="false" /><div class="draft-actions"><span>已恢复方案 v{{ restoredSpec.version }} · {{ confirmed ? '本界面已确认' : '待确认' }}</span><button class="ui-button primary sm" :disabled="!confirmable || confirmed" @click="confirm">确认方案</button></div></template>
              <p v-if="confirmed" class="pane-note">方案已在本浏览器确认；此确认不是后端审批。修改或恢复版本后需重新确认。</p>
            </div>
            <div class="workspace-composer"><p v-if="legacyOnly" class="pane-note">此历史单镜头任务没有作品级 Project。原审批与恢复仍在任务记录中；点击“新作品”进入作品级创作。</p><MessageComposer ref="composer" :disabled="busy || hasRunning || loading || legacyOnly" @send="text => execute(text)" /><small>发送会生成或更新导演方案；不会自动进入生图。对话内容由关联任务的真实输入与公开结果恢复。</small></div>
          </section>
        </Pane>
      </Splitpanes>
    </div>
  </section>
</template>

<style scoped>
.director-workspace { display:flex; flex-direction:column; height:100%; min-height:0; background:var(--surface); color:var(--text-primary); }
.workspace-toolbar { display:flex; align-items:center; gap:12px; flex-wrap:wrap; height:auto; min-height:48px; flex-shrink:0; box-sizing:border-box; padding:10px 16px; border-bottom:1px solid var(--border); font-size:13px; }
.workspace-toolbar > strong { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:100px; }
.workspace-toolbar label { display:flex; align-items:center; gap:7px; }
select { color:var(--text-primary); background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-control); padding:6px; font:inherit; max-width:100%; }
.workspace-execution { color:var(--text-secondary); font-size:12px; }
.workspace-body { display:flex; flex:1; min-height:0; }
.workspace-navigation { width:48px; flex-shrink:0; padding:8px 4px; border-right:1px solid var(--border); background:var(--canvas); }
.workspace-navigation.expanded { width:116px; }
.workspace-navigation button { display:flex; align-items:center; gap:10px; width:100%; padding:10px; margin-bottom:5px; border:0; border-radius:var(--radius-control); background:transparent; color:var(--text-secondary); font:inherit; font-size:13px; text-align:left; }
.workspace-navigation button[aria-current], .stage-navigation button[aria-current] { background:var(--canvas-inset); color:var(--accent); }
.workspace-navigation button:disabled { color:var(--text-muted); opacity:.5; cursor:not-allowed; }
.workspace-split { min-width:0; flex:1; }
.workspace-split :deep(.splitpanes__splitter) { height:6px; min-height:6px; background:var(--canvas-inset); border-block:1px solid var(--border); cursor:row-resize; }
.workspace-split :deep(.splitpanes__splitter:hover) { background:var(--border-strong); }
.workspace-upper { display:flex; height:100%; min-height:0; }
.workspace-stage { display:flex; flex:1; min-width:0; min-height:0; flex-direction:column; }
.stage-heading, .conversation-toolbar { display:flex; align-items:center; gap:10px; padding:10px 18px; flex-shrink:0; border-bottom:1px solid var(--border); }
.stage-heading h2 { font-size:15px; flex:1; margin:0; }
.stage-navigation { display:flex; gap:4px; padding:8px 14px; border-bottom:1px solid var(--border); overflow-x:auto; flex-shrink:0; }
.stage-navigation button { background:transparent; color:var(--text-secondary); border:0; padding:7px 10px; border-radius:var(--radius-control); font-size:12px; flex-shrink:0; }
.stage-navigation small { display:block; font-size:11px; margin-top:4px; }
.stage-scroll { flex:1; overflow:auto; padding:18px max(20px, calc((100% - 800px) / 2)); font-size:14px; line-height:1.65; }
.stage-scroll h3 { font-size:15px; }
.stage-scroll details { padding:12px 0; border-bottom:1px solid var(--border); }
.stage-scroll summary { cursor:pointer; } .stage-scroll dd { margin:4px 0 14px; white-space:pre-wrap; }
.workspace-inspector { width:240px; box-sizing:border-box; padding:14px; border-left:1px solid var(--border); overflow:auto; font-size:12px; background:var(--canvas); }
.workspace-inspector header { display:flex; justify-content:space-between; align-items:center; }
.workspace-inspector dd { margin:4px 0 14px; overflow-wrap:anywhere; color:var(--text-secondary); }
.workspace-conversation { display:flex; flex-direction:column; height:100%; min-height:0; }
.conversation-toolbar { padding:6px 18px; font-size:12px; }
.conversation-toolbar span { flex:1; } .conversation-toolbar small { color:var(--text-muted); }
.workspace-messages { flex:1; min-height:0; overflow:auto; padding:18px max(20px, calc((100% - 820px) / 2)); }
.creative-turn { margin-bottom:24px; } .conversation-welcome { color:var(--text-secondary); font-size:14px; line-height:1.8; max-width:650px; }
.workspace-composer { padding:8px max(20px, calc((100% - 820px) / 2)) 10px; flex-shrink:0; }
.workspace-composer > small { display:block; margin-top:6px; color:var(--text-muted); font-size:11px; }
.draft-actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:12px 0; font-size:12px; }
.draft-actions span { display:flex; gap:5px; align-items:center; color:var(--text-secondary); }
.asset-group, .shot-row, .history-row { padding-bottom:14px; margin-bottom:18px; border-bottom:1px solid var(--border); }
.history-row > small { display:block; color:var(--text-muted); }
.history-task { display:block; background:transparent; color:var(--text-primary); border:0; padding:10px 0; width:100%; text-align:left; }
.history-task small { display:block; color:var(--text-muted); overflow-wrap:anywhere; }
.review-notice { color:var(--text-secondary); border-left:2px solid var(--accent); padding-left:12px; }
.workspace-error { color:var(--danger); overflow-wrap:anywhere; }
button:focus-visible, select:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
@media(max-width:800px) { .workspace-inspector { width:180px; } .workspace-toolbar { gap:6px; } .workspace-execution { display:none; } }
@media(max-width:600px) { .workspace-toolbar > strong { flex-basis:100%; } .workspace-navigation.expanded { width:96px; } .workspace-inspector { width:150px; } .stage-scroll, .workspace-messages { padding:12px; } .workspace-composer { padding:6px 12px; } .workspace-composer > small { display:none; } }
</style>
