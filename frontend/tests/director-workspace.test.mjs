import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { reactive } from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'

const source = readFileSync(new URL('../src/domains/comic/directorPresentation.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const presentationUrl = `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`
const { discardDirectorNodeDraft, directorConversationSummary, directorPageFields, directorPageFieldLabel, fastDirectorNodeLabels, directorDraftFields, editableDirectorDraft, canConfirmDirector, canDispatchDirectorInput, chronologicalDirectorExecutions, directorIsStale, directorSummary, stageDraftKey, publicDirectorSections, workspaceProjectTitle } = await import(presentationUrl)
const spec = {
  schema_version: 2, spec_id: 'director', version: 2, creative_brief_version: 3,
  creative_decision: { intent_summary: '异兽观察文明', emotional_target: '孤独', private_thought: 'must not show' },
  director_plan: { visual_strategy: '留出环境空间', composition_strategy: '人物与城市遥相呼应' },
  cinematography: { camera_angle: '平视', light_direction: '侧光', status: 'complete' }, critic_result: { verdict: 'pass', reviewed_spec_hash: 'reviewed' },
  asset_versions: { 'asset:character': 1 },
}
test('both modes edit public draft fields without changing identity or hard constraints', () => {
  const original = structuredClone({ ...spec, creative_decision: { ...spec.creative_decision, hard_constraints: ['异兽'] } })
  const fields = directorDraftFields(original)
  assert.equal(fields['creative_decision.emotional_target'], '孤独')
  assert.equal(fields['creative_decision.hard_constraints'], undefined)
  assert.equal(fields['creative_decision.private_thought'], undefined)
  const saved = editableDirectorDraft(original, { 'creative_decision.emotional_target': '期待' })
  assert.equal(saved.creative_decision.emotional_target, '期待')
  assert.deepEqual(saved.creative_decision.hard_constraints, ['异兽'])
  assert.equal(saved.critic_result, null)
  assert.equal(saved.version, undefined)
  assert.equal(saved.user_confirmed, undefined)
  assert.equal(original.creative_decision.emotional_target, '孤独')
  assert.throws(() => editableDirectorDraft(original, { 'creative_decision.hard_constraints': '人物' }))
})
test('v2 public projection does not expose legacy fields or private model metadata', () => {
  const output = directorSummary({ ...spec, emotion: 'legacy emotion', camera_language: 'legacy camera' })
  assert.match(output, /异兽观察文明/)
  assert.doesNotMatch(output, /legacy|must not show/)
  assert.equal(publicDirectorSections(spec).length, 3)
  assert.equal(directorSummary({ emotion: '旧方案' }), '')
})
test('only real completed reviewed v2 can be confirmed; draft changes invalidate', () => {
  assert.equal(canConfirmDirector('completed', spec, false, false), true)
  for (const status of ['pending', 'running', 'failed']) assert.equal(canConfirmDirector(status, spec, false, false), false)
  assert.equal(canConfirmDirector('completed', spec, true, false), false)
  assert.equal(canConfirmDirector('completed', spec, false, true), false)
  assert.equal(canConfirmDirector('waiting', { ...spec, critic_result: { verdict: 'needs_revision', reviewed_spec_hash: 'reviewed', findings: [{ severity: 'warning', code: 'ARTISTIC_ADVICE' }] } }, false, false), true)
  for (const code of ['HARD_CONSTRAINT_CONFLICT', 'REVIEW_EXECUTION_FAILED', 'CINEMATOGRAPHY_INCOMPLETE']) {
    assert.equal(canConfirmDirector('waiting', { ...spec, critic_result: { verdict: 'needs_revision', reviewed_spec_hash: 'reviewed', findings: [{ severity: code === 'HARD_CONSTRAINT_CONFLICT' ? 'error' : 'warning', code }] } }, false, false), false)
  }
  assert.equal(canConfirmDirector('completed', { ...spec, schema_version: 1 }, false, false), false)
})
test('Vue reactive drafts can be copied and saved without DataCloneError or changing the source', () => {
  const original = reactive(structuredClone(spec))
  const saved = editableDirectorDraft(original, { 'creative_decision.emotional_target': '期待' })
  assert.equal(saved.creative_decision.emotional_target, '期待')
  assert.equal(original.creative_decision.emotional_target, '孤独')
  assert.equal(saved.critic_result, null)
})
test('dependency changes mark old spec stale; locked assets retain their referenced version', () => {
  const assets = [{ asset_id: 'character', version: 1, pinned_version: null, state: 'active' }]
  assert.equal(directorIsStale(spec, 3, 2, assets), false)
  assert.equal(directorIsStale(spec, 4, 2, assets), true)
  assert.equal(directorIsStale(spec, 3, 3, assets), true)
  assert.equal(directorIsStale(spec, 3, 2, []), true)
  assert.equal(directorIsStale(spec, 3, 2, [{ ...assets[0], version: 2 }]), true)
  assert.equal(directorIsStale(spec, 3, 2, [{ ...assets[0], version: 2, pinned_version: 1 }]), false)
  assert.equal(directorIsStale(spec, 3, 2, [{ ...assets[0], state: 'deleted' }]), true)
})
test('stage drafts remain independent across project, Run, and node navigation', () => {
  const drafts = {}
  const key = stageDraftKey('project', 'run', 'visual_direction')
  drafts[key] = { visual_strategy: '保留远古异兽的孤独感' }
  drafts[stageDraftKey('project', 'run', 'cinematography')] = { camera_angle: '平视' }
  assert.equal(drafts[key].visual_strategy, '保留远古异兽的孤独感')
  assert.equal(drafts[stageDraftKey('other', 'run', 'visual_direction')], undefined)
  assert.equal(drafts[stageDraftKey('project', 'new-run', 'visual_direction')], undefined)
})
test('a new task cannot insert an orphan assistant turn before its persisted user input arrives', () => {
  const tasks = [{ run_id: 'new' }, { run_id: 'old' }]
  const records = { old: { started_at: '2026-09-28T08:00:00Z' } }
  assert.deepEqual(chronologicalDirectorExecutions(tasks, records), [tasks[1]])
  records.new = { started_at: '2026-09-28T09:00:00Z' }
  assert.deepEqual(chronologicalDirectorExecutions(tasks, records), [tasks[1], tasks[0]])
  assert.deepEqual(tasks.map(task => task.run_id), ['new', 'old'])
})
test('workspace reuses chat and existing APIs without a second workflow or asset Drawer', () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const parent = readFileSync(new URL('../src/views/ProductionWorkspace.vue', import.meta.url), 'utf8')
  assert.match(view, /conversation_id: conversation/)
  assert.match(view, /UserMessageBubble/)
  assert.match(view, /MessageComposer/)
  assert.doesNotMatch(view, /Splitpanes|<Pane(?:\s|>)|stage-navigation|chatSize/)
  assert.match(view, /class="director-flow" aria-label="导演流程"/)
  assert.match(view, /navOpen = ref\(!window.matchMedia/)
  assert.match(view, /inspectorOpen = ref\(false\)/)
  assert.match(view, /:disabled="!confirmed"/)
  assert.match(view, /id: 'conversation', label: '对话'/)
  assert.match(view, /id: 'director', label: '导演'/)
  assert.match(view, /id: 'storyboard', label: '分镜'/)
  assert.match(view, /id: 'prompt', label: 'Prompt'/)
  assert.match(view, /id: 'assets', label: '资产'/)
  assert.match(view, /id: 'history', label: '历史'/)
  assert.doesNotMatch(view, /id: 'creative'|id: 'works'|原始创意/)
  assert.match(view, /if \(id === 'conversation'\) \{[\s\S]*chatExpanded\.value = true/)
  assert.doesNotMatch(view, /streamConversationMessage|images\/generations/)
  assert.doesNotMatch(parent, /comic-flyout|directorWorkspaceOpen|assetsOpen/)
  assert.match(view, /restoredSpec\.value \? undefined : active\.value\?\.director_execution_summary/)
  assert.match(view, /ownsConversationRun\(run, id\)/)
  assert.match(view, /board\?\.director_spec_version !== spec\.value\?\.version/)
  assert.match(view, /restoreChoice === Number\(version\.version\)/)
  assert.doesNotMatch(view, /latestVersion && !tasks.some/)
  assert.match(view, /creative_operation: 'new'/)
  assert.match(view, /spec\.value\?\.user_confirmed === true/)
  assert.match(view, /confirmDirectorVersion/)
  assert.doesNotMatch(view, /kantoku-director-confirmation:/)
  assert.match(view, /if \(failedRun\) \{ selectedRun\.value = failedRun\.run_id/)
})

test('confirmed workspace submits version-bound production through existing Run API', () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  assert.match(view, /async function generateImage\(\)/)
  assert.match(view, /if \(!productionEligible\.value/)
  assert.match(view, /const manualDirectorApproval = ref\(true\)/)
  assert.match(view, /approval_required: manualDirectorApproval\.value/)
  assert.match(view, /manualDirectorApproval\.value && !confirmed\.value/)
  assert.match(view, /createRun\('comic', \{/)
  assert.match(view, /production_project_id: project\.value\.project\.project_id/)
  assert.match(view, /director_version: spec\.value\.version/)
  assert.match(view, /request_id: productionRequests\.get\(key\)/)
  assert.match(view, /@click="generateImage"/)
  assert.match(view, /name: 'tasks', tab: 'waiting'/)
  assert.doesNotMatch(view, /provider\.submit|images\/generations/)
})

test('Chat gives a concise entry to the real draft, never another technical director report', () => {
  const text = directorConversationSummary({ ...spec, director_plan: { visual_strategy: 'do not repeat the report' }, cinematography: { camera_angle: 'hidden camera' } })
  assert.match(text, /异兽观察文明/)
  assert.match(text, /草稿/)
  assert.doesNotMatch(text, /do not repeat|hidden camera|must not show/)
  assert.equal(directorConversationSummary(null), '')
  assert.equal(directorConversationSummary({ schema_version: 1, emotion: 'old' }), '')
  assert.match(directorConversationSummary({ ...spec, version: undefined }), /尚未保存/)
})

test('Fast photography hides technical fields, while Professional preserves the same data', () => {
  const body = { public_decision: '让人物与环境共同进入画面', shot_size: '远景', camera_angle: '低机位', lighting: '侧光', continuity_rules: ['人物不变'], private_thought: 'hidden' }
  assert.deepEqual(directorPageFields('cinematography', body, 'fast'), { public_decision: body.public_decision, shot_size: '远景' })
  assert.equal(directorPageFields('cinematography', body, 'professional').camera_angle, '低机位')
  assert.equal(directorPageFields('cinematography', body, 'professional').lighting, '侧光')
})

test('fixed shared input is outside node switching; returning from Chat preserves the selected node', () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const { descriptor, errors } = parse(view)
  assert.deepEqual(errors, [])
  assert.match(descriptor.template.content, /<WorkspaceShell/)
  assert.match(descriptor.template.content, /<template #composer>/)
  assert.doesNotMatch(descriptor.template.content, /<footer class="workspace-composer"/)
  assert.equal((descriptor.template.content.match(/<MessageComposer /g) ?? []).length, 1)
  assert.match(view, /if \(page && page !== 'conversation'\)/)
  assert.match(view, /v-show="chatExpanded" ref="timeline"/)
  assert.match(view, /if \(!text.trim\(\)\) return\s+chatExpanded.value = true/)
  const modifyAction = view.slice(view.indexOf('function reviseByInstruction'), view.indexOf('async function saveFinal'))
  assert.doesNotMatch(modifyAction, /composer.value\?\.fill\(''\)/)
  assert.doesNotMatch(view, /scrollIntoView|location.hash|role="tab"|role="tablist"/)
})

// Render the actual shared node component, not a test-only copy of its layout.
const nodeSource = readFileSync(new URL('../src/components/DirectorNodeView.vue', import.meta.url), 'utf8')
const nodeScript = compileScript(parse(nodeSource).descriptor, { id: 'workspace-node-contract', inlineTemplate: true }).content
const nodeModule = ts.transpileModule(nodeScript, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  .replaceAll("'../domains/comic/directorPresentation'", JSON.stringify(presentationUrl))
  .replace(/from (["'])vue\1/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`)
  .replace(/from (["'])lucide-vue-next\1/g, `from ${JSON.stringify(import.meta.resolve('lucide-vue-next'))}`)
const { default: NodeView } = await import(`data:text/javascript;base64,${Buffer.from(nodeModule).toString('base64')}`)
const renderNode = props => renderToString(createSSRApp(NodeView, { node: undefined, spec, editing: false, fields: {}, editable: true, rerunnable: false, busy: false, ...props }))

test('actual node page renders only the selected node and mode-appropriate controls', async () => {
  const saved = { ...spec, cinematography: { public_decision: '人物与环境同框', shot_size: '远景', camera_angle: '低角度', lighting: '逆光' } }
  const fast = await renderNode({ spec: saved, stage: 'cinematography', mode: 'fast' })
  const pro = await renderNode({ spec: saved, stage: 'cinematography', mode: 'professional' })
  assert.match(fast, /人物与环境同框|画面范围/)
  assert.doesNotMatch(fast, /低角度|逆光|异兽观察文明|留出环境空间/)
  assert.match(pro, /低角度|逆光|编辑节点/)
  const edit = await renderNode({ stage: 'visual_direction', mode: 'professional', editing: true, fields: { 'director_plan.composition_strategy': '局部修改', 'cinematography.camera_angle': '别的节点' } })
  assert.match(edit, /textarea|局部修改|保存草稿版本/)
  assert.doesNotMatch(edit, /别的节点/)
})

test('final draft is a navigable approval index, not stacked report cards', async () => {
  const html = await renderNode({ stage: 'director_assemble', mode: 'professional' })
  assert.match(html, /方案组成|plan-index/)
  assert.doesNotMatch(html, /plan-card|node-fields|textarea/)
  assert.equal((html.match(/<button/g) ?? []).length, 3)
})
test('Fast keeps only creative and director summary; v2 optional fields and narrative context are public', () => {
  const current = { ...spec, creative_decision: { narrative_context: '远古异兽观察文明' },
    director_plan: { character_presence: '克制而孤独', style_boundary: null },
    cinematography: { camera_language: '平视长焦观察' } }
  const fields = publicDirectorSections(current)
  assert.equal(fields[0].fields.narrative_context, '远古异兽观察文明')
  assert.equal(fields[1].fields.character_presence, '克制而孤独')
  assert.equal('style_boundary' in fields[1].fields, false)
  assert.match(directorSummary(current, 'professional'), /摄影方案|平视长焦/)
  assert.doesNotMatch(directorSummary(current, 'fast'), /摄影方案|平视长焦/)
})
test('toolbar does not repeat an auto-generated raw request title', () => {
  assert.equal(workspaceProjectTitle('我想制作一张穷奇', '我想制作一张穷奇站在悬崖'), '漫剧作品')
  assert.equal(workspaceProjectTitle('暮色的守望者', '我想制作一张穷奇'), '暮色的守望者')
})
test('queued supplements never start while a Run is busy or outcome is unknown', () => {
  const ready = { busy: false, running: false, loading: false, error: false, cancelling: false }
  assert.equal(canDispatchDirectorInput(ready), true)
  for (const key of Object.keys(ready)) assert.equal(canDispatchDirectorInput({ ...ready, [key]: true }), false)
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  assert.match(view, /:disabled="loading \|\| legacyOnly" @send="sendInput"/)
  assert.match(view, /queuedInputs\.value\.push\(\{ id: \+\+nextInputId, text, mode: mode\.value \}\)/)
  assert.match(view, /await execute\(next.text, \{\}, next.mode\)/)
  assert.match(view, /await cancelRun\(task.run_id\)/)
  assert.match(view, /prefers-reduced-motion:reduce/)
})

test('Fast pages expose only approachable fields; Professional keeps all public decisions', () => {
  const body = { intent_summary: '少女眺望村庄', audience_experience: '遥远的乡愁', emotional_target: '安静', private_thought: 'hidden', hard_constraints: ['少女'] }
  assert.deepEqual(directorPageFields('creative_decision', body, 'fast'), { intent_summary: body.intent_summary, emotional_target: '安静', hard_constraints: ['少女'] })
  assert.equal(directorPageFields('creative_decision', body, 'professional').audience_experience, '遥远的乡愁')
  assert.equal(directorPageFields('creative_decision', body, 'professional').private_thought, undefined)
  assert.equal(directorPageFieldLabel('cinematography', 'shot_size', 'fast'), '画面范围')
  assert.equal(directorPageFieldLabel('cinematography', 'shot_size', 'professional'), '景别')
  assert.equal(fastDirectorNodeLabels.director_critic, undefined)
  assert.equal(fastDirectorNodeLabels.cinematography, '镜头感觉')
})

test('independent node pages share one version-bound editor and one persistent conversation', () => {
  const workspace = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const page = readFileSync(new URL('../src/components/DirectorNodeView.vue', import.meta.url), 'utf8')
  const app = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
  assert.match(workspace, /navigate\(\{ \.\.\.route.value, workspacePage, directorStage \}\)/)
  assert.doesNotMatch(workspace, /scrollIntoView|location.hash/)
  assert.match(workspace, /spec.value\?\.schema_version === 2 \? 'director_assemble' : selectedStage.value/)
  assert.match(workspace, /scrollPositions.set\(old, stageScroll.value.scrollTop\)/)
  assert.match(workspace, /scrollPositions.get\(key\)/)
  assert.equal((workspace.match(/<MessageComposer /g) ?? []).length, 1)
  assert.match(app, /:key="`\$\{route.domain\}:\$\{route.runId \?\? 'new'\}`"/)
  assert.match(page, /Saved v2 is the current editable version/)
  assert.match(page, /stage === 'director_assemble'/)
  assert.match(page, /section === sectionKey.value/)
  assert.match(page, /mode !== 'fast'/)
})
test('discarding one node preserves other edits; saving uses the same immutable draft version', () => {
  const edits = { 'creative_decision.emotional_target': '希望', 'director_plan.composition_strategy': '环境留白', 'cinematography.camera_angle': '侧面平视' }
  const remaining = discardDirectorNodeDraft(edits, 'visual_direction')
  assert.deepEqual(remaining, { 'creative_decision.emotional_target': '希望', 'cinematography.camera_angle': '侧面平视' })
  const next = editableDirectorDraft(spec, remaining)
  assert.equal(next.creative_decision.emotional_target, '希望')
  assert.equal(next.cinematography.camera_angle, '侧面平视')
  assert.equal(next.director_plan.composition_strategy, spec.director_plan.composition_strategy)
  assert.equal(spec.cinematography.camera_angle, '平视')
  assert.equal(next.critic_result, null)
})

test('automatic workspace submits one durable production Run, not a director-only task', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf('async function execute('), view.indexOf('\nfunction sendInput('))
  const code = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const ref = value => ({ value })
  const submissions = []
  const state = {
    busy: ref(false), hasRunning: ref(false), project: ref(null), composer: ref(null),
    draftKey: ref('draft'), mode: ref('professional'), activeConversationId: ref('current'),
    manualDirectorApproval: ref(false), pendingText: ref(''), previousRunIds: ref([]),
    executions: ref([]), error: ref(''), restoredSpec: ref(null), productionRun: ref(null),
    runs: ref({}), section: ref('director'), selectedStage: ref('director_assemble'),
    invalidate() {}, failureText: String, refresh() {}, loadConversations() {},
    createDirectorExecution() { throw new Error('automatic mode must not stop at director analysis') },
    async createRun(domain, payload) {
      submissions.push({ domain, payload })
      return { id: 'production', state: { quick_creation: { project_id: 'fresh-project' } } }
    },
    async getComicProject(id) { return { project: { project_id: id } } },
  }
  const names = Object.keys(state)
  const execute = new Function('state', `const {${names.join(',')}} = state; let pendingCreation=null,productionAdvanced=false; const conversationEpoch=1,disposed=false; ${code}; return execute`)(state)
  await execute('少女竹林')
  assert.equal(submissions.length, 1)
  assert.deepEqual({ ...submissions[0].payload, request_id: 'stable-id' }, {
    creative_request: '少女竹林', conversation_id: 'current', creation_mode: 'professional',
    approval_required: false, request_id: 'stable-id',
  })
  assert.equal(state.productionRun.value.id, 'production')
  assert.equal(state.pendingText.value, '')
  assert.match(view, /manualDirectorApproval = ref\(true\)/)
  assert.match(view, /manualDirectorApproval.value = !latest \|\| !productionRun.value/)
  assert.match(view, /v-if="manualDirectorApproval && spec && selectedStage === 'director_assemble'"/)
  assert.match(view, /productionRun.value.id\), getEvents\(productionRun.value.id\)/)
  assert.match(view, /if \(run\) runs.value\[run.id\] = run/)
  assert.match(view, /quick_creation as Record<string, unknown> \| undefined\)\?\.director_run_id === entry.execution.run_id/)
  assert.match(view, /section.value = 'storyboard'/)
  assert.match(view, /turn.artifactId && referenceUrls\[turn.artifactId\]/)
  assert.match(view, /productionProgress\?\.visible/)
})

test('professional confirmation enables a persistent transition to storyboard, never image controls in director', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const director = view.slice(view.indexOf(`<template v-if="section === 'director'`), view.indexOf(`<template v-else-if="section === 'assets'`))
  assert.doesNotMatch(director, /生成当前画面|@click="generateImage"|openSection\('prompt'\)/)
  assert.match(director, /@click="enterStoryboard"/)
  assert.match(view, /const confirmed = computed\(\(\) => !stale.value && !dirty.value/)
  const approval = view.slice(view.indexOf('const confirmed ='), view.indexOf('const productionEligible'))
  assert.doesNotMatch(approval, /confirmable.value|busy.value|hasRunning.value/)
  assert.match(approval, /status === 'approved'/)
  const body = view.slice(view.indexOf('async function enterStoryboard'), view.indexOf('async function restore('))
  const code = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const ref = value => ({ value })
  const state = { confirmed: ref(false), productionEligible: ref(true), busy: ref(false), hasRunning: ref(false) }
  const actions = []
  const enter = new Function('state', 'actions', `const {confirmed,productionEligible,busy,hasRunning}=state; function openSection(value){actions.push(value)}; async function generateImage(){actions.push('production')}; ${code}; return enterStoryboard`)(state, actions)
  await enter()
  assert.deepEqual(actions, [])
  state.confirmed.value = true
  await enter()
  assert.deepEqual(actions, ['storyboard', 'production'])
  assert.match(view, /section === 'storyboard' && productionEligible/)
  const production = view.slice(view.indexOf('async function generateImage'), view.indexOf('async function enterStoryboard'))
  assert.match(production, /productionRun.value = run/)
  assert.match(production, /openSection\('storyboard'\)/)
  assert.doesNotMatch(production, /navigate\(\{ name: 'task_run'/)
})
