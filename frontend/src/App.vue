<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { getState, loadArtwork, startAction } from './api'
import ApprovalPanel from './components/ApprovalPanel.vue'
import EmptyState from './components/EmptyState.vue'
import QcPanel from './components/QcPanel.vue'
import StatusBadge from './components/StatusBadge.vue'
import WorkflowProgress, { type WorkflowStep } from './components/WorkflowProgress.vue'
import CommandPalette from './components/layout/CommandPalette.vue'
import SidebarNav from './components/layout/SidebarNav.vue'
import TopBar from './components/layout/TopBar.vue'
import { domainById, domains, type DomainDefinition } from './domains'
import type { GenerationSettings, StudioForm, StudioState, StudioTask } from './types'
import AssetsView from './views/AssetsView.vue'
import HomeView from './views/HomeView.vue'
import RunsView from './views/RunsView.vue'
import WorkspaceView from './views/WorkspaceView.vue'

type RouteName = 'dashboard' | 'workspace' | 'runs' | 'run_detail' | 'assets' | 'history' | 'skills' | 'settings' | 'domain'
type NoticeTone = 'normal' | 'success' | 'error'
interface RouteState { name: RouteName; param?: string }
const emptyState: StudioState = { tasks: [], budgets: {}, job: { state: 'idle' }, config: { image: '—', chat: '—', vision: '—', size: '—', limit: '20', estimate_fen: 300 } }
const defaultForm: StudioForm = { project: '未命名 Production', shot_no: 1, purpose: '叙事静帧', subject: '', style: '自然光、克制色彩、纪实摄影', audience: '普通观众', prompt: '', price: '3.00' }
const state = ref<StudioState>(emptyState)
const route = ref<RouteState>(parseRoute())
const activeDomainId = ref('studio')
const form = reactive<StudioForm>({ ...defaultForm })
const generation = reactive<GenerationSettings>({ model: '', ratio: '3:4', resolution: '2K', quality: 'standard', quantity: 1 })
const selectedId = ref<string | null>(null)
const promptApproved = ref(false)
const promptVersion = ref(1)
const artworkUrls = reactive<Record<string, string>>({})
const notice = reactive<{ text: string; tone: NoticeTone }>({ text: 'Kantoku Core 已连接本机工作区。', tone: 'normal' })
const refreshing = ref(false)
const lastJobId = ref('')
const sidebarCollapsed = ref(false)
const commandOpen = ref(false)
const runDetailTab = ref<'overview' | 'workflow' | 'qc' | 'approval'>('overview')
const modal = reactive({ open: false, title: '', message: '', confirmLabel: '确认', cancelLabel: '取消', input: false, value: '' })
let timer: number | undefined
let resolveModal: ((value: boolean) => void) | null = null

const selected = computed(() => state.value.tasks.find((task) => task.request_id === selectedId.value) ?? null)
const busy = computed(() => state.value.job.state === 'running')
const activeDomain = computed(() => domainById(activeDomainId.value))
const currentBudget = computed(() => state.value.budgets[form.project.trim()] ?? { project: form.project, task_count: 0, settled_fen: 0, held_fen: 0, unknown_count: 0, unbilled_count: 0, limit_fen: Math.round(Number(state.value.config.limit) * 100), available_fen: Math.round(Number(state.value.config.limit) * 100) })
const estimatedFen = computed(() => state.value.config.estimate_fen ?? Math.max(1, Math.round(Number(form.price) * 100)))
const allSkills = computed(() => domains.flatMap((domain) => domain.skills.map((skill) => ({ ...skill, domain }))))
const runDetail = computed(() => state.value.tasks.find((task) => task.request_id === route.value.param) ?? null)
/* 候选：同一项目 + 同一镜号下所有真实生成过的产物。每次「再次生成」都会产生一个新候选，
   网格按真实候选数自适应 —— 不伪造到 4 张，因为当前接口 quantity 固定为 1。 */
const candidates = computed(() => {
  const current = selected.value
  if (!current) return []
  return state.value.tasks
    .filter((task) => task.has_image && task.project === current.project && task.shot_no === current.shot_no)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .map((task) => ({ task, url: artworkUrls[task.request_id] as string | undefined }))
})
const workflowSteps = computed<WorkflowStep[]>(() => buildWorkflow(selected.value))

function parseRoute(): RouteState { const path = window.location.pathname.replace(/\/+$/, '') || '/'; if (path === '/') return { name: 'dashboard' }; if (path === '/workspace') return { name: 'workspace' }; if (path === '/runs') return { name: 'runs' }; if (path.startsWith('/runs/')) return { name: 'run_detail', param: decodeURIComponent(path.slice(6)) }; if (path === '/assets') return { name: 'assets' }; if (path === '/history') return { name: 'history' }; if (path === '/skills') return { name: 'skills' }; if (path === '/settings') return { name: 'settings' }; if (path.startsWith('/domain/')) return { name: 'domain', param: path.slice(8) }; return { name: 'dashboard' } }
function navigate(path: string): void { if (window.location.pathname !== path) window.history.pushState({}, '', path); route.value = parseRoute(); if (route.value.name === 'domain' && route.value.param) activeDomainId.value = route.value.param; window.scrollTo({ top: 0, behavior: 'smooth' }) }
function chooseDomain(domain: DomainDefinition): void { activeDomainId.value = domain.id; navigate(`/domain/${domain.id}`) }
function openDomain(id: string): void { chooseDomain(domainById(id)) }
function quickStart(domain: DomainDefinition): void { activeDomainId.value = domain.id; if (domain.status === 'available') { resetWorkspace(); navigate('/workspace') } else { navigate(`/domain/${domain.id}`); setNotice(`${domain.name} 当前为 ${domain.status === 'preview' ? 'Preview' : 'Coming Soon'}，不会伪造执行。`) } }
function startCreation(requirementText: string): void { resetWorkspace(); form.subject = requirementText; activeDomainId.value = 'studio'; navigate('/workspace'); setNotice('需求已填入工作台，确认上下文后可生成 Prompt。') }
function routeTitle(): string { return ({ dashboard: '首页', workspace: '工作台', runs: '任务与运行', run_detail: '运行详情', assets: '资产', history: '历史记录', skills: '技能', settings: '设置', domain: activeDomain.value.name } as Record<RouteName, string>)[route.value.name] }
function setNotice(text: string, tone: NoticeTone = 'normal'): void { notice.text = text; notice.tone = tone }
function formatFen(value: number | null): string { return value === null ? '—' : `¥${(value / 100).toFixed(2)}` }
function formatTime(raw: string): string { if (!raw) return '—'; const value = new Date(raw.replace(' ', 'T') + 'Z'); return Number.isNaN(value.valueOf()) ? raw : value.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) }
function taskStatus(task: StudioTask): string { if (task.status === 'failed') return 'failed'; if (task.archived) return 'completed'; if (task.status === 'unknown' || task.has_image || task.rework?.status === 'pending') return 'waiting'; if (task.status === 'succeeded') return 'running'; return 'pending' }
function buildWorkflow(task: StudioTask | null): WorkflowStep[] { return [{ id: 'plan', name: '需求规划', status: task?.prompt || form.subject || form.prompt ? 'completed' : 'pending', detail: '需求与交付目标' }, { id: 'prompt', name: 'Prompt 构建', status: task?.prompt || form.prompt ? 'completed' : 'pending', detail: `Prompt v${promptVersion.value}` }, { id: 'generate', name: '图像生成', status: busy.value && state.value.job.action === 'generate' ? 'running' : task?.has_image ? 'completed' : task?.status === 'failed' ? 'failed' : 'pending', detail: task?.has_image ? 'Artifact 已生成' : '等待提交' }, { id: 'qc', name: '质量检查', status: task?.qc ? 'completed' : task?.has_image ? 'waiting' : 'pending', detail: task?.qc ? 'VLM 预筛完成' : '等待 Artifact' }, { id: 'review', name: '人工审批', status: task?.review ? 'completed' : task?.qc ? 'waiting' : 'pending', detail: task?.review ? (task.review.approved ? '已批准' : '未通过') : '等待人工决定' }, { id: 'delivery', name: '交付归档', status: task?.archived ? 'completed' : task?.review?.approved ? 'waiting' : 'pending', detail: task?.archived ? '已归档' : '等待归档' }] }
function payload(): Record<string, unknown> { return { project: form.project.trim(), shot_no: Number(form.shot_no), purpose: form.purpose, subject: form.subject, style: form.style, audience: form.audience, prompt: form.prompt, price: (estimatedFen.value / 100).toFixed(2), request_id: selected.value?.request_id, confirmed: true, model: generation.model || state.value.config.image, ratio: generation.ratio, resolution: generation.resolution, quality: generation.quality, quantity: generation.quantity, prompt_version: promptVersion.value } }
function ask(title: string, message: string, options: { confirmLabel?: string; cancelLabel?: string; input?: boolean } = {}): Promise<boolean> { Object.assign(modal, { open: true, title, message, confirmLabel: options.confirmLabel ?? '确认', cancelLabel: options.cancelLabel ?? '取消', input: options.input ?? false, value: '' }); return new Promise((resolve) => { resolveModal = resolve }) }
function closeModal(value: boolean): void { modal.open = false; resolveModal?.(value); resolveModal = null }
async function run(action: string, body: Record<string, unknown> = payload()): Promise<void> { if (busy.value) return; try { await startAction(action, body); setNotice(action === 'generate' ? '生成任务已提交，预算保护已生效。' : '操作已提交，正在等待结果。'); await refresh() } catch (error) { setNotice(error instanceof Error ? error.message : '操作没有完成', 'error') } }
async function generate(): Promise<void> { if (activeDomain.value.id !== 'studio') { setNotice('该 Domain 尚未连接可执行 Workflow。', 'error'); return }; if (!form.prompt.trim() || !promptApproved.value) { setNotice('请先填写并批准 Prompt。', 'error'); return }; if (await ask('确认生成 Artifact？', `将通过 ${state.value.config.image} 生成 1 张图片，预计预占 ${formatFen(estimatedFen.value)}。`, { confirmLabel: '确认生成' })) await run('generate') }
async function composePrompt(): Promise<void> { await run('compose') }
async function refinePrompt(): Promise<void> { if (!form.prompt.trim()) { setNotice('请先填写 Prompt。', 'error'); return }; if (await ask('重新生成 Prompt？', '会调用已配置文本模型，不会生成图片。', { confirmLabel: '重新生成' })) await run('refine') }
function approvePrompt(): void { promptApproved.value = true; promptVersion.value += 1; setNotice(`Prompt v${promptVersion.value} 已批准。`, 'success') }
async function runQc(): Promise<void> { if (selected.value?.has_image && await ask('运行质量检查？', `将调用 ${state.value.config.vision}。VLM 只做预筛，最终决定仍由人工完成。`, { confirmLabel: '运行 QC' })) await run('qc') }
async function decideReview(decision: 'approve' | 'reject' | 'request_revision', reason: string, notes: string): Promise<void> { if (!selected.value?.qc) return; const labels = { approve: '批准当前 Artifact', reject: '拒绝当前 Artifact', request_revision: '创建返修草稿' }; if (await ask(labels[decision], decision === 'request_revision' ? '不会自动提交新的付费生成。' : '该人工决定会持久化且不可静默覆盖。', { confirmLabel: labels[decision] })) await run('review', { ...payload(), decision, approved: decision === 'approve', failure_reasons: decision === 'approve' ? [] : [reason], cinematography_requirements: form.style, cinematography_notes: notes.trim() || selected.value.qc.reason }) }
async function archiveSelected(): Promise<void> { if (selected.value?.review && await ask('归档 Artifact？', '将保存原图、来源与质量元数据。', { confirmLabel: '确认归档' })) await run('archive') }
async function recoverSelected(): Promise<void> { if (selected.value) await run('recover') }
async function resumeSelected(): Promise<void> { if (selected.value && await ask('继续原任务？', '只会恢复原请求，不会建立重复任务。', { confirmLabel: '继续' })) await run('resume') }
async function settleSelected(): Promise<void> { if (!selected.value || !(await ask('回填平台实扣', '只填写平台已确认的最终金额。', { confirmLabel: '保存账单', input: true }))) return; if (modal.value === '' || Number(modal.value) < 0) { setNotice('请输入非负金额。', 'error'); return }; await run('settle', { ...payload(), price: modal.value }) }
async function selectTask(task: StudioTask, destination = '/workspace'): Promise<void> { selectedId.value = task.request_id; form.project = task.project; form.shot_no = task.shot_no; form.prompt = task.prompt; promptApproved.value = true; activeDomainId.value = 'studio'; await ensurePreview(task); navigate(destination) }
function resetWorkspace(): void { Object.assign(form, defaultForm); selectedId.value = null; promptApproved.value = false; promptVersion.value = 1 }
async function ensurePreview(task: StudioTask): Promise<void> { if (!task.has_image || artworkUrls[task.request_id]) return; try { artworkUrls[task.request_id] = await loadArtwork(task.request_id) } catch { /* Run Detail 展示真实素材错误 */ } }
async function handleCompletedJob(snapshot: StudioState): Promise<void> { const job = snapshot.job; if (!job.id || job.id === lastJobId.value || job.state === 'running') return; lastJobId.value = job.id; if (job.state === 'error') { setNotice(job.error ?? '操作没有完成', 'error'); return }; const result = job.result ?? {}; if (result.prompt) { form.prompt = result.prompt; promptApproved.value = false; promptVersion.value += 1; setNotice(`Prompt v${promptVersion.value} 已生成，等待人工批准。`, 'success') }; if (result.request_id) { const task = snapshot.tasks.find((item) => item.request_id === result.request_id); if (task) { selectedId.value = task.request_id; await ensurePreview(task) } }; if (result.qc) setNotice(`QC 完成：${result.qc.reason}`, 'success'); if (result.message) setNotice(result.message, 'success') }
async function refresh(): Promise<void> { if (refreshing.value) return; refreshing.value = true; try { const snapshot = await getState(); state.value = snapshot; generation.model ||= snapshot.config.image; if (route.value.name === 'run_detail' && route.value.param && !selectedId.value) { const task = snapshot.tasks.find((item) => item.request_id === route.value.param); if (task) { selectedId.value = task.request_id; form.project = task.project; form.shot_no = task.shot_no; form.prompt = task.prompt; promptApproved.value = true } }; await handleCompletedJob(snapshot); await Promise.all(snapshot.tasks.filter((task) => task.has_image).slice(0, 12).map(ensurePreview)) } catch (error) { setNotice(`本机服务暂不可用：${error instanceof Error ? error.message : '未知错误'}`, 'error') } finally { refreshing.value = false } }
function handleShortcut(event: KeyboardEvent): void { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); commandOpen.value = !commandOpen.value } }
function handlePopState(): void { route.value = parseRoute() }
watch(() => form.prompt, () => { promptApproved.value = false })
onMounted(() => { window.addEventListener('popstate', handlePopState); window.addEventListener('keydown', handleShortcut); if (route.value.name === 'domain' && route.value.param) activeDomainId.value = route.value.param; void refresh(); timer = window.setInterval(() => void refresh(), 1800) })
onBeforeUnmount(() => { window.removeEventListener('popstate', handlePopState); window.removeEventListener('keydown', handleShortcut); if (timer !== undefined) window.clearInterval(timer); Object.values(artworkUrls).forEach((url) => URL.revokeObjectURL(url)) })
</script>

<template>
  <div class="kantoku-shell" :class="{ 'sidebar-collapsed': sidebarCollapsed }">
    <SidebarNav :route-name="route.name" :active-domain-id="activeDomainId" :task-count="state.tasks.length" :collapsed="sidebarCollapsed" @navigate="navigate" @domain="chooseDomain" @toggle="sidebarCollapsed = !sidebarCollapsed" />
    <section class="app-stage"><TopBar :title="routeTitle()" :busy="busy" @command="commandOpen = true" />
      <Transition name="route" mode="out-in"><div :key="`${route.name}:${route.param ?? ''}`" class="route-stage">
      <HomeView v-if="route.name === 'dashboard'" :tasks="state.tasks" :busy="busy" @navigate="navigate" @quick-start="quickStart" @open-task="selectTask" @create="startCreation" />
      <WorkspaceView v-else-if="route.name === 'workspace'" :domain="activeDomain" :form="form" :generation="generation" :task="selected" :busy="busy" :approved="promptApproved" :version="promptVersion" :workflow="workflowSteps" :budget="currentBudget" :config="state.config" :estimated-fen="estimatedFen" :artwork-url="selected ? artworkUrls[selected.request_id] : undefined" :candidates="candidates" @open-candidate="(task) => void selectTask(task)" @reset="resetWorkspace" @switch-studio="activeDomainId = 'studio'" @compose="composePrompt" @refine="refinePrompt" @approve="approvePrompt" @generate="generate" @recover="recoverSelected" @resume="resumeSelected" @precheck="runQc" @decide="decideReview" @archive="archiveSelected" />
      <RunsView v-else-if="route.name === 'runs'" :tasks="state.tasks" @open="selectTask" @create="navigate('/workspace')" />
      <AssetsView v-else-if="route.name === 'assets'" :tasks="state.tasks" :artwork-urls="artworkUrls" @open="selectTask" />
      <main v-else-if="route.name === 'run_detail'" class="page run-detail-page"><header class="page-header"><div><button class="back-link" @click="navigate('/runs')">← 任务与运行</button><h1>{{ runDetail?.project ?? '未找到运行' }}</h1><p v-if="runDetail">{{ runDetail.request_id }}</p></div><StatusBadge v-if="runDetail" :status="taskStatus(runDetail)" /></header><template v-if="runDetail"><nav class="detail-tabs"><button v-for="item in ([['overview','摘要'],['workflow','工作流'],['qc','质检'],['approval','审批']] as const)" :key="item[0]" :class="{ active: runDetailTab === item[0] }" @click="runDetailTab = item[0]">{{ item[1] }}</button></nav><section v-if="runDetailTab === 'overview'" class="surface detail-summary"><header class="panel-header"><div><span class="section-kicker">运行详情</span><h2>执行摘要</h2></div></header><dl><div><dt>创作域</dt><dd>Studio</dd></div><div><dt>工作流</dt><dd>图片生产</dd></div><div><dt>预估</dt><dd>{{ formatFen(runDetail.estimate_fen) }}</dd></div><div><dt>实扣</dt><dd>{{ formatFen(runDetail.actual_fen) }}</dd></div><div><dt>账本</dt><dd>{{ runDetail.ledger_status }}</dd></div><div><dt>产物</dt><dd>{{ runDetail.has_image ? 1 : 0 }}</dd></div></dl><div v-if="runDetail.error" class="error-box">{{ runDetail.error }}</div><div class="detail-actions"><button class="ui-button" @click="selectTask(runDetail)">打开 Workspace</button><button class="ui-button" @click="settleSelected">回填账单</button></div></section><section v-else-if="runDetailTab === 'workflow'" class="surface detail-tab-panel"><WorkflowProgress :steps="buildWorkflow(runDetail)" /></section><section v-else-if="runDetailTab === 'qc'" class="surface detail-tab-panel"><QcPanel :qc="runDetail.qc" /></section><section v-else class="surface detail-tab-panel"><ApprovalPanel :task="runDetail" :busy="busy" @precheck="runQc" @decide="decideReview" @archive="archiveSelected" /></section></template><EmptyState v-else title="未找到运行" description="该运行不存在或已经从本机记录中移除。" /></main>
      <main v-else-if="route.name === 'history'" class="page"><header class="page-header"><div><span class="section-kicker">历史</span><h1>历史记录</h1><p>按时间查看真实 Production Run、费用与 Artifact 状态。</p></div></header><section class="timeline-history"><article v-for="task in state.tasks" :key="task.request_id"><span></span><div class="surface"><header><div><strong>{{ task.project }} · #{{ task.shot_no }}</strong><small>Studio · 图片生产</small></div><StatusBadge :status="taskStatus(task)" /></header><p>{{ task.prompt }}</p><footer><time>{{ formatTime(task.created_at) }}</time><b>{{ formatFen(task.actual_fen ?? task.estimate_fen) }}</b><button class="text-action" @click="selectTask(task, `/runs/${encodeURIComponent(task.request_id)}`)">查看 Trace →</button></footer></div></article><EmptyState v-if="!state.tasks.length" title="暂无历史" description="运行历史会来自真实任务记录。" /></section></main>
      <main v-else-if="route.name === 'skills'" class="page"><header class="page-header"><div><span class="section-kicker">技能注册表</span><h1>技能</h1><p>Skill 描述如何完成任务；Tool 负责具体模型或 API 能力。</p></div><span class="count-chip">{{ allSkills.length }} 已注册</span></header><section class="skill-grid"><article v-for="item in allSkills" :key="`${item.domain.id}-${item.id}`" class="surface skill-card"><header><span class="domain-icon">{{ item.domain.icon }}</span><StatusBadge :status="item.status" /></header><span class="section-kicker">{{ item.domain.name }}</span><h2>{{ item.name }}</h2><p>{{ item.description }}</p><footer><span>所需工具</span><div><b v-for="tool in item.requiredTools" :key="tool">{{ tool }}</b></div></footer></article></section></main>
      <main v-else-if="route.name === 'settings'" class="page"><header class="page-header"><div><span class="section-kicker">模型配置</span><h1>模型与供应商</h1><p>当前配置来自本机 settings；API Key 不在页面中显示。</p></div></header><section class="settings-grid"><article v-for="provider in [{ icon: 'LLM', label: '文本 / Prompt', name: state.config.chat, note: '统一文本模型出口' }, { icon: 'IMG', label: '图像生成', name: state.config.image, note: state.config.size }, { icon: 'VLM', label: '视觉 / 质检', name: state.config.vision, note: '质量预筛；人工终审保留' }]" :key="provider.icon" class="surface provider-card"><span class="provider-icon">{{ provider.icon }}</span><div><small>{{ provider.label }}</small><h2>{{ provider.name }}</h2><p>{{ provider.note }}</p></div><StatusBadge status="available" /></article></section><article class="surface security-note"><span>KEY</span><div><h2>凭据仅保存在本机</h2><p>密钥只从本机 `.env` 读取，本页面不会返回、显示或记录明文凭据。</p></div></article></main>
      <main v-else class="page domain-page"><header class="page-header"><div><span class="section-kicker">创作域</span><h1>{{ activeDomain.name }} · {{ activeDomain.label }}</h1><p>{{ activeDomain.description }}</p></div><StatusBadge :status="activeDomain.status" /></header><div class="domain-detail-grid"><article class="surface"><header class="panel-header"><div><span class="section-kicker">工作流</span><h2>可用工作流</h2></div></header><ul class="definition-list"><li v-for="workflow in activeDomain.workflows" :key="workflow"><span>◇</span><strong>{{ workflow }}</strong></li><li v-if="!activeDomain.workflows.length"><span>—</span><strong>尚未登记 Workflow</strong></li></ul></article><article class="surface"><header class="panel-header"><div><span class="section-kicker">能力</span><h2>能力组合</h2></div></header><div class="capability-list"><span v-for="capability in activeDomain.capabilities" :key="capability">{{ capability }}</span><p v-if="!activeDomain.capabilities.length">待上线</p></div></article></div><section class="surface domain-action"><div><h2>{{ activeDomain.status === 'available' ? '开始使用当前 Domain' : '当前不提供伪执行' }}</h2><p>{{ activeDomain.status === 'available' ? '进入统一 Workspace 创建真实 Run。' : 'Domain 定义可见，但外部 Adapter 与 Workflow 尚未完成。' }}</p></div><button class="ui-button primary" :disabled="activeDomain.status !== 'available'" @click="quickStart(activeDomain)">进入 Workspace</button></section></main>
      </div></Transition>
      <div class="toast" :data-tone="notice.tone"><i></i><span>{{ notice.text }}</span></div><footer class="product-footer"><span>ONE CORE. UNLIMITED CREATIONS.</span><span>KANTOKU · LOCAL PRODUCTION SYSTEM</span></footer>
    </section>
    <CommandPalette :open="commandOpen" @close="commandOpen = false" @navigate="navigate" @domain="openDomain" />
    <div v-if="modal.open" class="modal-backdrop" @click.self="closeModal(false)"><section class="modal"><span class="section-kicker">人工确认</span><h2>{{ modal.title }}</h2><p>{{ modal.message }}</p><label v-if="modal.input">平台最终实扣 / 元<input v-model="modal.value" class="ui-input" type="number" min="0" step="0.01" /></label><footer><button class="ui-button" @click="closeModal(false)">{{ modal.cancelLabel }}</button><button class="ui-button primary" @click="closeModal(true)">{{ modal.confirmLabel }}</button></footer></section></div>
  </div>
</template>
