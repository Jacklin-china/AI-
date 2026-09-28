<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { directorFieldLabels, directorNodeLabels, editableDirectorNode, selectDirectorExecution, visibleDirectorNodes } from '../domains/comic/directorPresentation'
import { createComicProject, createDirectorExecution, getComicProject, getDirectorExecutions, type ComicProjectContext, type CreationMode, type DirectorExecution } from '../services/core'

const project = ref<ComicProjectContext | null>(null)
const mode = ref<CreationMode>('fast')
const executions = ref<DirectorExecution[]>([])
const selectedRun = ref('')
const selectedStage = ref('')
const request = ref('')
const busy = ref(false)
const previousRunIds = ref<string[]>([])
const loading = ref(true)
const error = ref('')
const editing = ref(false)
const editFields = ref<Record<string, string>>({})
let timer: ReturnType<typeof setInterval> | null = null
let disposed = false
let refreshing = false
const active = computed(() => selectDirectorExecution(executions.value, selectedRun.value, busy.value, previousRunIds.value))
const summary = computed(() => active.value?.director_execution_summary)
const nodes = computed(() => summary.value ? visibleDirectorNodes(summary.value.mode, summary.value.stages) : [])
const node = computed(() => summary.value?.stages.find(item => item.stage === selectedStage.value))
const hasRunning = computed(() => executions.value.some(item => ['running', 'pending'].includes(item.status) && !item.recovery_required))
const editable = computed(() => node.value && editableDirectorNode(node.value.stage) && summary.value?.available_actions.includes('edit_stage'))
const stateLabels: Record<string, string> = { pending: '待执行', running: '执行中', completed: '已完成', failed: '失败', waiting: '需要调整' }
const body = computed(() => node.value ? Object.values(node.value.output)[0] ?? {} : {})

async function refresh(): Promise<void> {
  if (!project.value || refreshing) return
  refreshing = true
  const id = project.value.project.project_id
  try {
    const [context, tasks] = await Promise.all([getComicProject(id), getDirectorExecutions(id)])
    if (disposed || id !== project.value?.project.project_id) return
    if (context) project.value = context
    executions.value = tasks
  } finally { refreshing = false }
}

async function execute(options: Record<string, unknown> = {}): Promise<void> {
  if (busy.value || hasRunning.value || (!project.value && !request.value.trim())) return
  busy.value = true
  previousRunIds.value = executions.value.map(item => item.run_id)
  error.value = ''
  const currentRequest = request.value.trim()
  try {
    if (!project.value) {
      project.value = await createComicProject(currentRequest)
      localStorage.setItem('kantoku-comic-project', project.value.project.project_id)
    }
    await refresh()
    const execution = await createDirectorExecution(project.value.project.project_id, {
      expected_project_version: project.value.project.current_version,
      creation_mode: mode.value,
      ...('previous_run_id' in options || 'resume_run_id' in options ? {} : { task: currentRequest || null }),
      ...options,
    })
    if (disposed) return
    selectedRun.value = execution.run_id
    selectedStage.value = ''
    editing.value = false
    if (request.value.trim() === currentRequest) request.value = ''
    await refresh()
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : '导演方案未完成'
    await refresh().catch(() => undefined)
  } finally { busy.value = false }
}

function beginEdit(): void {
  editFields.value = Object.fromEntries(Object.entries(body.value)
    .filter(([key, value]) => key !== 'hard_constraints' && (typeof value === 'string' || Array.isArray(value)))
    .map(([key, value]) => [key, Array.isArray(value) ? value.join('\n') : String(value)]))
  editing.value = true
}
async function rerun(saveEdit = false): Promise<void> {
  if (!active.value || !node.value) return
  mode.value = 'professional'
  const options: Record<string, unknown> = { previous_run_id: active.value.run_id, rerun_from: node.value.stage }
  if (saveEdit) {
    const patch = { ...body.value }
    for (const [key, text] of Object.entries(editFields.value)) {
      patch[key] = Array.isArray(body.value[key]) ? text.split('\n').map(item => item.trim()).filter(Boolean) : text
    }
    options.stage_edits = { [node.value.stage]: patch }
  }
  await execute(options)
}
async function resume(): Promise<void> {
  if (!active.value || !summary.value) return
  mode.value = summary.value.mode
  await execute({ resume_run_id: active.value.run_id })
}
function newProject(): void {
  if (busy.value || hasRunning.value) return
  localStorage.removeItem('kantoku-comic-project')
  project.value = null
  executions.value = []
  selectedRun.value = ''
  selectedStage.value = ''
  error.value = ''
}
watch(selectedStage, () => { editing.value = false })
watch(selectedRun, () => { selectedStage.value = ''; editing.value = false })
onMounted(async () => {
  try {
    const id = localStorage.getItem('kantoku-comic-project')
    if (id) {
      project.value = await getComicProject(id)
      await refresh()
      if (summary.value) mode.value = summary.value.mode
    }
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '作品载入失败' }
  finally { loading.value = false }
  timer = setInterval(() => { void refresh().catch(failure => {
    error.value = failure instanceof Error ? failure.message : '状态查询失败'
  }) }, 1200)
})
onBeforeUnmount(() => { disposed = true; if (timer) clearInterval(timer) })
</script>

<template>
  <section class="director-workspace" aria-label="作品导演工作区">
    <header class="director-toolbar">
      <strong>{{ project?.project.title ?? '新的创作想法' }}</strong>
      <label class="director-mode">创作模式
        <select v-model="mode" :disabled="busy || hasRunning || loading">
          <option value="fast">普通模式</option><option value="professional">专业导演模式</option>
        </select>
      </label>
      <button class="ui-button quiet sm" :disabled="busy || hasRunning" @click="newProject">新作品</button>
    </header>
    <div class="director-content">
      <p v-if="loading" role="status">正在载入作品</p>
      <p v-else-if="busy && !active" role="status">正在提交创作需求</p>
      <p v-else-if="!active" class="pane-note">描述人物、故事或想表达的感受。AI 将理解创意并设计导演方案，无需填写镜头参数。</p>
      <template v-if="active && summary">
        <div class="director-status" role="status" aria-live="polite">
          <strong>{{ summary.status_label }}</strong>
          <span>{{ stateLabels[active.status] ?? active.status }}</span>
        </div>
        <p v-if="active.recovery_required" class="pane-note">上次执行已中断。可以恢复已完成步骤；系统不会自动重发模型请求。</p>
        <button v-if="summary.available_actions.includes('resume') || active.recovery_required" class="ui-button sm" :disabled="busy" @click="resume">恢复导演任务</button>
        <nav v-if="nodes.length" class="director-nodes" aria-label="导演节点">
          <button v-for="item in nodes" :key="item.stage" :aria-pressed="selectedStage === item.stage" @click="selectedStage = item.stage">
            {{ directorNodeLabels[item.stage] }}<small>{{ stateLabels[summary.stages.find(stage => stage.stage === item.stage)?.status ?? 'pending'] }}</small>
          </button>
        </nav>
        <section v-if="node && summary.mode === 'professional'" class="director-node-detail">
          <header><h3>{{ directorNodeLabels[node.stage] }}</h3>
            <button v-if="editable && !editing" class="ui-button quiet sm" :disabled="busy" @click="beginEdit">修改方案</button>
            <button v-if="summary.available_actions.includes('rerun_stage')" class="ui-button sm" :disabled="busy" @click="rerun()">从此节点重跑</button>
          </header>
          <p>{{ node.output_summary ?? '此节点尚无完成结果。' }}</p>
          <details><summary>输入版本</summary><dl><div v-for="(version, key) in node.input_versions" :key="key"><dt>{{ key }}</dt><dd>{{ version }}</dd></div></dl></details>
          <template v-if="editing">
            <label v-for="(value, key) in editFields" :key="key" class="director-edit-field">{{ directorFieldLabels[key] ?? key }}<textarea v-model="editFields[key]" rows="3" /></label>
            <p class="pane-note">用户硬约束不可在这里修改。保存后重新执行后续节点，并再次审核。</p>
            <button class="ui-button primary sm" :disabled="busy" @click="rerun(true)">保存并重新审核</button>
            <button class="ui-button quiet sm" @click="editing = false">取消</button>
          </template>
          <dl v-else class="director-decisions"><div v-for="(value, key) in body" :key="key"><dt>{{ directorFieldLabels[key] ?? key }}</dt><dd>{{ Array.isArray(value) ? value.join('；') : typeof value === 'object' ? '' : value }}</dd></div></dl>
          <section v-if="node.stage === 'director_critic' && summary.critic_result">
            <strong>{{ summary.critic_result.verdict === 'pass' ? '审核通过' : '需要修订' }}</strong>
            <p>{{ summary.critic_result.public_summary }}</p>
            <p v-for="finding in summary.critic_result.findings" :key="finding.code + finding.evidence">{{ finding.suggested_action }}</p>
          </section>
        </section>
        <section v-else-if="active.director_spec" class="director-decisions" aria-label="导演方案摘要">
          <h3>导演方案</h3>
          <p>{{ active.director_spec.storytelling_goal }}</p>
          <p>{{ active.director_spec.visual_direction }}</p>
          <p>{{ active.director_spec.composition }}</p>
          <p class="pane-note">导演方案已保存。后续按作品分镜和镜头编译 Prompt；本阶段没有自动生成图片。</p>
        </section>
        <p v-if="summary.error_id" role="alert">错误编号：{{ summary.error_id }}</p>
        <details v-if="executions.length > 1"><summary>导演任务历史</summary><button v-for="item in executions" :key="item.run_id" class="ui-button quiet sm" @click="selectedRun = item.run_id">{{ item.director_execution_summary.mode === 'fast' ? '普通模式' : '专业模式' }} · {{ stateLabels[item.status] }}</button></details>
      </template>
      <p v-if="error" class="director-error" role="alert">{{ error }}</p>
    </div>
    <form class="director-composer" @submit.prevent="execute()">
      <label class="sr-only" for="director-request">创作需求</label>
      <textarea id="director-request" v-model="request" maxlength="1000" rows="3" placeholder="告诉 AI 你的故事与创意方向…" />
      <button class="ui-button primary" :disabled="busy || hasRunning || loading || !request.trim()">{{ busy || hasRunning ? '方案执行中' : '设计导演方案' }}</button>
    </form>
  </section>
</template>

<style scoped>
.director-workspace { display:flex; flex-direction:column; height:100%; min-height:0; color:var(--text-primary); background:var(--surface); }
.director-toolbar { display:flex; align-items:center; flex-wrap:wrap; gap:12px; padding:12px 20px; border-bottom:1px solid var(--border); }
.director-toolbar strong { flex:1; min-width:130px; font-size:13px; }
.director-mode { display:flex; align-items:center; gap:8px; color:var(--text-secondary); font-size:12px; }
.director-mode select { padding:6px; border:1px solid var(--border); border-radius:var(--radius-control); background:var(--surface); color:var(--text-primary); }
.director-content { flex:1; overflow:auto; padding:24px max(20px, calc((100% - 800px) / 2)); line-height:1.65; }
.director-status { display:flex; align-items:center; gap:12px; margin-bottom:16px; }
.director-status span, .director-nodes small { color:var(--text-muted); font-size:11px; }
.director-nodes { display:flex; flex-wrap:wrap; gap:6px; margin:20px 0; }
.director-nodes button { display:grid; gap:3px; padding:8px 12px; border:1px solid var(--border); border-radius:var(--radius-control); background:transparent; color:var(--text-secondary); }
.director-nodes button[aria-pressed="true"] { color:var(--accent); border-color:var(--accent); }
.director-node-detail header { display:flex; align-items:center; flex-wrap:wrap; gap:10px; }
.director-node-detail h3 { flex:1; font-size:15px; }
.director-decisions { margin:16px 0; }
.director-decisions > div { margin:14px 0; }
.director-decisions dt { font-weight:600; }
.director-decisions dd { margin:3px 0 0; white-space:pre-wrap; }
.director-edit-field { display:grid; gap:5px; margin:16px 0; font-size:13px; }
.director-edit-field textarea, .director-composer textarea { width:100%; resize:vertical; padding:10px 12px; font:inherit; color:var(--text-primary); border:1px solid var(--border); border-radius:var(--radius-control); }
.director-composer { display:flex; align-items:flex-end; gap:12px; margin:0 auto; padding:12px 20px 18px; width:min(100%, 840px); box-sizing:border-box; }
.director-error { color:var(--danger); }
button:focus-visible, select:focus-visible, textarea:focus-visible { outline:2px solid var(--accent); outline-offset:3px; }
@media(max-width:600px) { .director-toolbar { padding:10px; gap:8px; } .director-content { padding:16px; } .director-composer { padding:10px; flex-wrap:wrap; } }
</style>
