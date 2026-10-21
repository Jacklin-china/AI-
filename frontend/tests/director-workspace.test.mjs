import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { computed, h, reactive, ref, watch } from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createRenderer, createSSRApp, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'

const source = readFileSync(new URL('../src/domains/comic/directorPresentation.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const presentationUrl = `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`
const { discardDirectorNodeDraft, directorConversationSummary, directorConversationExecutions, selectDirectorExecution, directorPageFields, directorPageFieldLabel, fastDirectorNodeLabels, directorDraftFields, editableDirectorDraft, canConfirmDirector, canDispatchDirectorInput, chronologicalDirectorExecutions, directorIsStale, directorSummary, stageDraftKey, publicDirectorSections, workspaceProjectTitle } = await import(presentationUrl)
const { conversationWorkspaceRun } = await import(presentationUrl)
const spec = {
  schema_version: 2, spec_id: 'director', version: 2, creative_brief_version: 3,
  creative_decision: { intent_summary: '异兽观察文明', emotional_target: '孤独', private_thought: 'must not show' },
  director_plan: { visual_strategy: '留出环境空间', composition_strategy: '人物与城市遥相呼应' },
  cinematography: { camera_angle: '平视', light_direction: '侧光', status: 'complete' }, critic_result: { verdict: 'pass', reviewed_spec_hash: 'reviewed' },
  asset_versions: { 'asset:character': 1 },
}
test('workspace restores direct image and story owners without inventing a director or crossing chats', () => {
  const run = (id, workflow, project = 'imported', owner = 'chat') => ({ id, workflow, state: { project_id: project, conversation_id: owner } })
  const image = run('import', 'comic.image.import'), story = run('story', 'comic.story.save')
  const foreign = run('other', 'comic.director', 'other-project', 'another-chat')
  assert.equal(conversationWorkspaceRun([foreign, image], 'chat'), image)
  assert.equal(conversationWorkspaceRun([foreign, story, image], 'chat', foreign.id), story)
  const director = run('director', 'comic.director', 'planned')
  assert.equal(conversationWorkspaceRun([story, director, image], 'chat'), director)
  assert.equal(conversationWorkspaceRun([story, director, image], 'chat', image.id), image)
  assert.equal(conversationWorkspaceRun([foreign], 'chat'), undefined)
  assert.equal(conversationWorkspaceRun([run('noise', 'studio.document')], 'chat'), undefined)
})
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
test('unavailable critic is explicit and confirmable without inventing a review', () => {
  const unavailable = { ...spec, critic_status: 'unavailable', critic_result: null }
  assert.equal(canConfirmDirector('waiting', unavailable, false, false), true)
  assert.equal(canConfirmDirector('waiting', unavailable, true, false), false)
  assert.equal(canConfirmDirector('waiting', unavailable, false, true), false)
  assert.equal(canConfirmDirector('waiting', { ...unavailable, cinematography: { status: 'missing' } }, false, false), false)
  assert.equal(canConfirmDirector('waiting', { ...unavailable, critic_status: null }, false, false), false)
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

test('reviewing a plan repeatedly never appends a duplicate creative reply, including legacy Runs', () => {
  const tasks = ['first', 'legacy-review', 'review-1', 'review-2', 'retry', 'second'].map(run_id => ({ run_id }))
  const records = Object.fromEntries(tasks.map((item, index) => [item.run_id, {
    started_at: `2026-10-08T10:00:0${index}Z`, state: { task: '同一条创作输入' },
  }]))
  records['legacy-review'].state.task = null
  records['review-1'].state.task_system = true
  records['review-2'].state.task_system = true
  records.retry.state.previous_run_id = 'first'
  assert.deepEqual(directorConversationExecutions(tasks, records).map(item => item.run_id), ['first', 'second'])
  assert.equal(chronologicalDirectorExecutions(tasks, records).length, 6)
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
  // 「节点详情」没有实际用途：入口与面板一起移除，工作区不再有第三列。
  assert.doesNotMatch(view, /inspectorOpen|workspace-inspector|节点详情/)
  assert.match(view, /manualDirectorApproval && !confirmed/)
  assert.match(view, /id: 'conversation', label: '对话'/)
  assert.match(view, /id: 'plan', label: '画面方案'/)
  assert.match(view, /id: 'story', label: '故事编排'/)
  assert.match(view, /id: 'prompt', label: '生图指令'/)
  assert.doesNotMatch(view, /id: 'assets', label: '作品素材'/)
  assert.doesNotMatch(view, /id: 'history', label: '版本历史'/)
  assert.doesNotMatch(view, /id: 'creative'|id: 'works'|原始创意/)
  assert.match(view, /if \(id === 'conversation'\) \{[\s\S]*chatExpanded\.value = true/)
  assert.doesNotMatch(view, /streamConversationMessage|images\/generations/)
  assert.doesNotMatch(parent, /comic-flyout|directorWorkspaceOpen|assetsOpen/)
  // 查看历史版本（restoredSpec）或作品当前稿比执行记录更新时，都不展示执行摘要，
  // 避免把某次 Run 的执行状态当成"当前方案"的状态。
  assert.match(view, /const summary = computed\(\(\) => restoredSpec\.value \|\| currentDirectorIsNewer\.value \? undefined : active\.value\?\.director_execution_summary\)/)
  assert.match(view, /currentDirectorIsNewer/)
  assert.match(view, /getCurrentDirector\(projectId\)/)
  assert.match(view, /ownsConversationRun\(run, id\)/)
  assert.match(view, /board\?\.director_spec_version !== spec\.value\?\.version/)
  assert.match(view, /restoreChoice === Number\(version\.version\)/)
  assert.doesNotMatch(view, /latestVersion && !tasks.some/)
  assert.match(view, /creative_operation: 'auto'/)
  assert.match(view, /spec\.value\?\.user_confirmed === true/)
  assert.match(view, /confirmDirectorVersion/)
  assert.doesNotMatch(view, /kantoku-director-confirmation:/)
  assert.match(view, /if \(failedRun\) \{ selectedRun\.value = failedRun\.run_id/)
})

test('new workspace prepares external Prompt through existing API without starting production', () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  assert.match(view, /const imageMode = 'external'/)
  assert.match(view, /const manualDirectorApproval = ref\(true\)/)
  assert.match(view, /compileComicPrompt\(shot.shot_id, project.value.project.current_version, shot.version, activeConversationId.value, true\)/)
  assert.match(view, /@click="compilePrompt"/)
  assert.doesNotMatch(view, /createRun\(|async function generateImage|provider\.submit|images\/generations/)
  assert.doesNotMatch(view, /name: 'tasks', tab: 'waiting'/)
})


test('Chat gives a concise entry to the real draft, never another technical director report', () => {
  const text = directorConversationSummary({ ...spec, director_plan: { visual_strategy: 'do not repeat the report' }, cinematography: { camera_angle: 'hidden camera' } })
  assert.match(text, /异兽观察文明/)
  assert.match(text, /草稿/)
  assert.doesNotMatch(text, /do not repeat|hidden camera|must not show/)
  assert.equal(directorConversationSummary(null), '')
  assert.equal(directorConversationSummary({ schema_version: 1, emotion: 'old' }), '')
  assert.match(directorConversationSummary({ ...spec, version: undefined }), /尚未保存/)
  const incomplete = directorConversationSummary({ ...spec, generation_incomplete: true })
  assert.match(incomplete, /内部检查与修正尚未完成/)
  assert.doesNotMatch(incomplete, /草稿已保存|补充修改方向/)
})

test('an internally incomplete candidate never marks the plan step saved or done', () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf('const productionSteps = computed'), view.indexOf('const activeRun = computed'))
  const code = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  for (const version of [undefined, 1]) {
    const state = { computed, spec: ref({ version, generation_incomplete: true }), workExecutions: ref([]),
      projectPrompts: ref([]), imageWorkbenchState: ref(null), project: ref(null), activeConversationId: ref('chat'),
      shots: ref([]), selectedShot: ref(''), externalPromptForShot: () => null,
      uploadedPrompts: ref([]), activeTask: ref(null), activeNavigation: ref('story'),
      storyWorkbench: { state: { view: null }, confirmed: ref(false) } }
    const steps = new Function('state', `const {${Object.keys(state).join(',')}}=state; ${code}; return productionSteps`)(state)
    assert.equal(steps.value[0].hint, '内部检查未完成')
    assert.equal(steps.value[0].status, 'todo')
    state.spec.value = { version: 1, generation_incomplete: false }
    assert.equal(steps.value[0].hint, '方案已保存')
    assert.equal(steps.value[0].status, 'done')
  }
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
const { directorFeedbackTarget } = await import(presentationUrl)

function mountNode(props) {
  const make = type => ({ type, props: {}, children: [], parent: null,
    focus() { this.focused = true }, scrollIntoView(options) { this.scrolled = options },
    querySelector(selector) { return all(this).find(node => selector === `textarea[data-director-field="${node.props['data-director-field']}"]`) },
  })
  const all = node => [node, ...node.children.flatMap(all)]
  const remove = node => { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1) }
  const renderer = createRenderer({
    createElement: make, createText: text => ({ ...make('#text'), text }), createComment: text => ({ ...make('#comment'), text }),
    setText(node, text) { node.text = text }, setElementText(node, text) { node.text = text; node.children = [] },
    patchProp(node, key, _previous, value) { node.props[key] = value },
    insert(node, parent, anchor) { remove(node); const index = parent.children.indexOf(anchor); parent.children.splice(index < 0 ? parent.children.length : index, 0, node); node.parent = parent },
    remove, parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1],
  })
  const root = make('root')
  const app = renderer.createApp(NodeView, { node: undefined, spec, editing: false, fields: {}, editable: true, rerunnable: false, busy: false, ...props })
  const instance = app.mount(root)
  return { instance, nodes: () => all(root), unmount: () => app.unmount() }
}

test('review suggestions copy usable text, report denied clipboard access, and emit exact field target', async () => {
  const finding = { level: 'warning', message: '机位建议', reason: '人物视线需要明确',
    affected_field: 'cinematography.camera_angle', affected_field_label: '机位', suggestion: '保持平视并朝向镜头', impact: '可确认' }
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
  const copied = [], located = []
  let denied = false
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { async writeText(text) { if (denied) throw new Error('denied'); copied.push(text) } } } })
  const mounted = mountNode({ stage: 'director_critic', spec: { ...spec, critic_feedback: [finding] }, onLocate: path => located.push(path) })
  try {
    await mounted.nodes().find(node => node.props.title === '复制建议').props.onClick()
    await nextTick()
    assert.deepEqual(copied, ['机位：保持平视并朝向镜头'])
    assert.ok(mounted.nodes().some(node => node.props.title === '已复制建议'))
    mounted.nodes().find(node => node.props.title === '定位修改：机位').props.onClick()
    assert.deepEqual(located, ['cinematography.camera_angle'])
    denied = true
    await mounted.nodes().find(node => node.props.title === '已复制建议').props.onClick()
    await nextTick()
    assert.ok(mounted.nodes().some(node => node.props.role === 'alert' && node.text.includes('复制失败')))
    assert.ok(!mounted.nodes().some(node => node.props.title === '已复制建议'))
  } finally {
    mounted.unmount()
    if (originalNavigator) Object.defineProperty(globalThis, 'navigator', originalNavigator)
    else delete globalThis.navigator
  }
})

test('locating a field reveals, focuses and highlights its editor without applying a suggestion', async () => {
  const originalWindow = globalThis.window
  globalThis.window = { matchMedia: () => ({ matches: true }) }
  const fields = directorDraftFields(spec), before = structuredClone(fields)
  const mounted = mountNode({ stage: 'cinematography', mode: 'fast', editing: true, fields })
  try {
    await mounted.instance.focusField('cinematography.camera_angle')
    const input = mounted.nodes().find(node => node.props['data-director-field'] === 'cinematography.camera_angle')
    assert.equal(input.focused, true)
    assert.equal(input.scrolled.block, 'center')
    assert.match(input.props.class, /located-field/)
    assert.deepEqual(fields, before)
    assert.equal(directorFeedbackTarget(spec, 'creative_decision.hard_constraints'), undefined)
    assert.equal(directorFeedbackTarget(spec, 'cinematography.status'), undefined)
    assert.equal(directorFeedbackTarget(spec, 'unknown.camera_angle'), undefined)
  } finally { mounted.unmount(); globalThis.window = originalWindow }
})

test('actual workspace suggestion navigation preserves draft fields and respects busy and stale guards', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf('function beginEdit('), view.indexOf('function editFinal('))
    + view.slice(view.indexOf('async function locateSuggestion('), view.indexOf('function reviseByInstruction('))
  const js = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const ref = value => ({ value }), focused = [], visited = []
  let stage = 'director_critic'
  const environment = { busy: ref(false), hasRunning: ref(false), stale: ref(false), spec: ref(spec),
    draftKey: ref('selected-plan'), drafts: ref({ 'selected-plan': { 'director_plan.composition_strategy': '尚未保存的构图' } }),
    selectedStage: { get value() { return stage } },
    editable: { get value() { return stage === 'cinematography' } }, editing: ref(true),
    fields: { get value() { return environment.drafts.value['selected-plan'] } },
    directorStageSections: { cinematography: 'cinematography' }, directorDraftFields, directorFeedbackTarget,
    visitStage(value) { stage = value; visited.push(value) }, nextTick,
    directorNodeView: ref({ async focusField(path) { focused.push(path) } }),
    invalidate() {},
  }
  const navigateSuggestion = Function(...Object.keys(environment), `${js}; return locateSuggestion`)(...Object.values(environment))
  await navigateSuggestion('cinematography.camera_angle')
  assert.deepEqual(visited, ['cinematography'])
  assert.deepEqual(focused, ['cinematography.camera_angle'])
  assert.equal(environment.fields.value['director_plan.composition_strategy'], '尚未保存的构图')
  assert.equal(environment.fields.value['cinematography.camera_angle'], '平视')
  await navigateSuggestion('creative_decision.hard_constraints')
  environment.busy.value = true
  await navigateSuggestion('cinematography.camera_angle')
  environment.busy.value = false; environment.stale.value = true
  await navigateSuggestion('cinematography.camera_angle')
  assert.equal(visited.length, 1)
})

test('first generation uses server validation for confirmation without rendering review advice', async () => {
  const first = { ...spec, generation_mode: 'FIRST_GENERATION', review_required: false,
    critic_result: null, critic_feedback: [], validation: { can_confirm: true, can_produce: false } }
  assert.equal(canConfirmDirector('completed', first, false, false), true)
  assert.equal(canConfirmDirector('completed', first, true, false), false)
  assert.equal(canConfirmDirector('completed', first, false, true), false)
  assert.equal(canConfirmDirector('completed', { ...first, validation: { can_confirm: false } }, false, false), false)
  assert.equal(canConfirmDirector('completed', { ...first, validation: undefined }, false, false), false)
  const html = await renderNode({ spec: first, stage: 'director_assemble', mode: 'professional' })
  assert.doesNotMatch(html, /审核建议|修改建议|审核通过|建议仅供参考|暂无真实审核结果/)
  const hidden = await renderNode({ stage: 'director_critic', mode: 'professional',
    spec: { ...first, critic_result: spec.critic_result, critic_feedback: [{ message: '内部优化建议', suggestion: '不应展示' }] },
    node: { status: 'completed', output: {} } })
  assert.match(hidden, /方案检查已完成/)
  assert.doesNotMatch(hidden, /内部优化建议|不应展示|暂无真实审核结果/)
})

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

test('actual Critic view explains blocking issue without internal enum in review and final draft', async () => {
  const finding = { level: 'blocking', issue_type: 'required_parameter',
    message: '机位尚未确定唯一执行方案', reason: '同时保留了低角度或平视两个候选',
    suggestion: '选择一个最终机位后重新审核', affected_field: 'cinematography.camera_angle',
    affected_field_label: '机位', impact: '影响镜头执行，需修改后确认' }
  const saved = { ...spec, critic_feedback: [finding], critic_result: {
    verdict: 'blocked', findings: [{ code: 'EXECUTION_DECISION_UNRESOLVED', evidence: 'internal' }],
  } }
  for (const stage of ['director_critic', 'director_assemble']) {
    const html = await renderNode({ stage, mode: 'professional', spec: saved })
    assert.match(html, /问题：|原因：|影响：|修改建议：/)
    assert.match(html, /机位尚未确定|两个候选|选择一个最终机位/)
    assert.doesNotMatch(html, /EXECUTION_DECISION_UNRESOLVED|required_parameter|internal/)
  }
  const fast = await renderNode({ stage: 'director_assemble', mode: 'fast', spec: saved })
  assert.match(fast, /问题：|两个候选|选择一个最终机位/)
  assert.doesNotMatch(fast, /EXECUTION_DECISION_UNRESOLVED/)
})

test('actual Critic view presents warning as optional and preserves final confirmation', async () => {
  const saved = { ...spec, critic_feedback: [{ level: 'warning', issue_type: 'color_advice',
    message: '色彩优化建议', reason: '人物与背景色彩接近', suggestion: '可增强色彩分离',
    affected_field: 'director_plan.color_strategy', affected_field_label: '色彩方案',
    impact: '可改善色彩，不阻止最终确认' }],
    critic_result: { verdict: 'needs_revision', reviewed_spec_hash: 'reviewed',
      findings: [{ code: 'ARTISTIC_ADVICE', severity: 'warning' }] } }
  const html = await renderNode({ stage: 'director_assemble', mode: 'professional', spec: saved })
  assert.match(html, /审核建议，可确认方案|不阻止最终确认|可增强色彩分离/)
  assert.doesNotMatch(html, /ARTISTIC_ADVICE|审核阻止继续/)
  assert.equal(canConfirmDirector('waiting', saved, false, false), true)
})

test('actual followup handler requests an independent plan and selects its owning project', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const start = view.indexOf('async function execute(')
  const body = view.slice(start, view.indexOf('\nfunction sendInput(', start))
  const js = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const ref = value => ({ value })
  const sent = [], loaded = []
  const environment = {
    busy: ref(false), hasRunning: ref(false), project: ref({ project: { project_id: 'project', current_version: 5 } }),
    composer: ref(null), draftKey: ref('draft'), activeConversationId: ref('current'), mode: ref('professional'),
    pendingText: ref(''), pendingRequestId: ref(''), previousRunIds: ref([]), executions: ref([]), error: ref(''), restoredSpec: ref(null),
    selectedRun: ref('director'), selectedBoard: ref('old-board'), selectedShot: ref('old-shot'), section: ref('director'),
    drafts: ref({}), invalidate() {}, submittingDirector: ref(false),
    directorConversationExecutions,
    async getComicProject(id) { return { project: { project_id: id, current_version: 5 } } },
    async createDirectorExecution(id, payload) {
      sent.push({ id, payload }); return { run_id: 'new-director', director_spec: { project_id: 'new-project' } }
    },
    runs: ref({}), async getRun() { return { id: 'new-director', state: { project_id: 'new-project' } } },
    async refresh() {}, async loadPage() { loaded.push('storyboard') },
    async loadConversationMessages(owner) { loaded.push(owner) }, async loadConversations() {},
    failureText: String, selectDirectorExecution() { return null },
  }
  const execute = new Function('environment', `const {${Object.keys(environment).join(',')}}=environment; const conversationEpoch=1,workSelectionEpoch=0,disposed=false,activeWork={value:null}; ${js}; return execute`)(environment)
  await execute('手持长剑战斗')
  assert.equal(sent[0].payload.creative_operation, 'auto')
  assert.equal(sent[0].payload.conversation_id, 'current')
  assert.equal(sent[0].payload.task, '手持长剑战斗')
  assert.equal(sent[0].payload.independent_plan, true)
  assert.equal(sent[0].payload.request_id, environment.pendingRequestId.value)
  assert.equal(environment.selectedRun.value, 'new-director')
  assert.equal(environment.project.value.project.project_id, 'new-project')
  assert.equal(environment.section.value, 'director')
  assert.deepEqual(loaded, [])
  assert.equal(environment.error.value, '')
  environment.executions.value = [{ run_id: 'latest-creative' }, { run_id: 'old-plan-revision' }]
  environment.runs.value = {
    'latest-creative': { started_at: '2026-10-09T01:00:00Z', state: { task: '新的独立创意', project_id: 'latest-plan' } },
    'old-plan-revision': { started_at: '2026-10-09T02:00:00Z', state: { task: '修改旧方案', revision_instruction: '修改旧方案', project_id: 'older-plan' } },
  }
  environment.project.value = { project: { project_id: 'older-plan', current_version: 5 } }
  await execute('下一轮普通创意')
  assert.equal(sent[1].id, 'older-plan') // The server selects memory; navigation is only a target hint.
  assert.equal(sent[1].payload.independent_plan, true)
})

test('saving or confirming a plan never switches the active source to the latest generated plan', () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf('const active = computed('), view.indexOf('const summary = computed('))
  const js = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const state = { computed, selectDirectorExecution, executions: ref([{ run_id: 'old' }, { run_id: 'latest' }]),
    selectedRun: ref('old'), submittingDirector: ref(false), busy: ref(false), previousRunIds: ref(['old']),
    // UX-010 引入的"作品当前稿"来源：本用例只关心 active 的选版行为，故置空。
    currentDirector: ref(null) }
  state.workExecutions = state.executions
  const active = new Function('state', `const {${Object.keys(state).join(',')}}=state; ${js}; return active`)(state)
  state.busy.value = true
  assert.equal(active.value.run_id, 'old')
  state.submittingDirector.value = true
  assert.equal(active.value.run_id, 'latest')
})

test('legacy blocked Shot continuation is a visible turn, while its review stays out of chat', () => {
  const records = {
    continued: { started_at: '2026-10-08T01:00:00Z', workflow: 'comic.shot.continue',
      state: { context_diff: { instruction: '她一脸充满爱意的看着我' } } },
    review: { started_at: '2026-10-08T02:00:00Z', workflow: 'comic.director',
      state: { context_diff: { instruction: '她一脸充满爱意的看着我' } } },
  }
  assert.deepEqual(directorConversationExecutions([{ run_id: 'continued' }, { run_id: 'review' }], records), [{ run_id: 'continued' }])
})

test('actual plan selection restores each project independently and rejects another conversation', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf('async function selectPlan('), view.indexOf('\nfunction failureText('))
  const js = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const ref = value => ({ value })
  const loaded = []
  const state = {
    works: ref([]), executions: ref([]), async syncCurrentDirector() {},
    activeWork: ref(null), ownsWorkspaceRun: (run, id) => run.state.conversation_id === id,
    runs: ref({ first: { state: { project_id: 'first-project', conversation_id: 'current' } },
      second: { state: { project_id: 'second-project', conversation_id: 'current' } },
      foreign: { state: { project_id: 'foreign-project', conversation_id: 'other' } } }),
    activeConversationId: ref('current'), project: ref({ project: { project_id: 'second-project' } }),
    boards: ref(['second-board']), shots: ref(['second-shot']), prompts: ref(['second-prompt']),
    versions: ref([1]), selectedBoard: ref('second-board'), selectedShot: ref('second-shot'),
    promptPreview: ref({}), assets: ref([]), selectedRun: ref('second'),
    restoredSpec: ref({ version: 1 }), revisionVersion: ref(1),
    invalidate() { state.revisionVersion.value = null },
    ownsConversationRun: (run, id) => run.state.conversation_id === id,
    async getComicProject(id) { loaded.push(id); return { project: { project_id: id } } },
    async getComicAssets(id) { return [id] }, async loadPage() {},
  }
  const selectPlan = new Function('state', `const {${Object.keys(state).join(',')}}=state; const conversationEpoch=1,disposed=false; ${js}; return selectPlan`)(state)
  await selectPlan('first')
  assert.equal(state.project.value.project.project_id, 'first-project')
  assert.equal(state.selectedRun.value, 'first')
  assert.deepEqual(state.shots.value, [])
  await selectPlan('second')
  assert.equal(state.project.value.project.project_id, 'second-project')
  await selectPlan('foreign')
  assert.equal(state.project.value.project.project_id, 'second-project')
  assert.deepEqual(loaded, ['first-project', 'second-project'])
})

test('a legacy image work cannot display the director of another Brief in the same project', () => {
  const source = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = source.slice(source.indexOf('const workExecutions = computed('), source.indexOf('const active = computed('))
  const code = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const state = { computed, executions: ref([
    { run_id: 'cowboy', director_spec: { creative_brief_version: 2 } },
    { run_id: 'foreign', director_spec: { creative_brief_version: 1 } },
  ]), runs: ref({ cowboy: { state: { project_id: 'same' } }, foreign: { state: { project_id: 'other' } } }),
  project: ref({ project: { project_id: 'same' } }), currentWork: ref({ has_director: false, brief_version: 1 }) }
  const filtered = new Function('state', `const {${Object.keys(state).join(',')}}=state; ${code}; return workExecutions`)(state)
  assert.deepEqual(filtered.value, [])
  state.currentWork.value = { has_director: true, brief_version: 2 }
  assert.deepEqual(filtered.value.map(item => item.run_id), ['cowboy'])
  state.currentWork.value = { has_director: true, brief_version: 1 }
  assert.deepEqual(filtered.value, [])
})

test('work selection clears scoped media and late selection cannot replace the newer work', async () => {
  const source = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = source.slice(source.indexOf('async function selectWork('), source.indexOf('\nfunction failureText('))
  const code = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  let release, delayNext = false
  const state = { works: ref([{ work_id: 'image', project_id: 'same', brief_version: 1, has_director: false },
    { work_id: 'plan', project_id: 'same', brief_version: 2, director_run_id: 'cowboy', has_director: true }]),
    project: ref(null), selectedWorkId: ref(''), selectedRun: ref(''), restoredSpec: ref({ version: 1 }), currentDirector: ref({}),
    boards: ref(['old']), shots: ref(['old']), prompts: ref(['old']), versions: ref([1]), assets: ref([]),
    projectPrompts: ref(['old']), shotPrompts: ref({ old: [] }), imageWorkbenchState: ref({ adopted: true }),
    selectedBoard: ref('old'), selectedShot: ref('old'), promptPreview: ref({}), promptEdit: ref({}),
    productionRun: ref({ id: 'old' }), productionEvents: ref([]), error: ref(''),
    invalidate() {}, failureText: String, async syncCurrentDirector() {}, async getComicAssets() { return [] }, async loadPage() {},
    async getComicProject(id) {
      if (delayNext) { delayNext = false; return new Promise(resolve => { release = resolve }) }
      return { project: { project_id: id } }
    } }
  const choose = new Function('state', `const {${Object.keys(state).join(',')}}=state; let workSelectionEpoch=0,pageRequest=0; const conversationEpoch=1,disposed=false; let productionAdvanced=false; ${code}; return selectWork`)(state)
  await choose('image')
  assert.equal(state.selectedRun.value, '')
  assert.equal(state.currentDirector.value, null)
  assert.deepEqual(state.projectPrompts.value, [])
  assert.equal(state.imageWorkbenchState.value, null)
  delayNext = true
  const old = choose('image')
  await Promise.resolve()
  await choose('plan')
  release({ project: { project_id: 'same' } })
  await old
  assert.equal(state.selectedRun.value, 'cowboy')
  assert.equal(state.selectedWorkId.value, 'plan')
  await choose('not-owned')
  assert.equal(state.selectedWorkId.value, 'plan')
})

function revisionWorkspace(save = async () => ({ project_id: 'plan-a', version: 2 })) {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf('function reviseByInstruction('), view.indexOf('async function reviewFinal('))
    + view.slice(view.indexOf('function sendInput('), view.indexOf('/* Title belongs', view.indexOf('function sendInput(')))
    + view.slice(view.indexOf('function invalidate('), view.indexOf('function beginEdit('))
  const js = ts.transpileModule(body.replace(/\bconversationEpoch\b/g, 'state.epoch'),
    { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const ref = value => ({ value }), requests = [], filled = [], newInputs = []
  const state = {
    epoch: 1, disposed: false, project: ref({ project: { project_id: 'plan-a', current_version: 2 } }),
    spec: ref({ ...spec, project_id: 'plan-a', version: 1 }), activeConversationId: ref('owner'),
    selectedRun: ref('run-a'), revisionVersion: ref(null), revisionTarget: ref(null),
    busy: ref(false), hasRunning: ref(false), dirty: ref(false), stale: ref(false),
    cancelling: ref(false), chatExpanded: ref(false), error: ref(''), restoredSpec: ref(null),
    draftKey: ref('a-v1'), drafts: ref({ 'other-plan': { 'director_plan.visual_focus': '未保存的其他方案' } }), fields: ref({}),
    executions: ref([{ run_id: 'saved-a', director_spec: { project_id: 'plan-a', version: 2 } }]),
    composer: ref({ fill(text) { filled.push(text) } }), queuedInputs: ref([]), mode: ref('professional'),
    editableDirectorDraft, failureText: failure => failure.message, titleConversation() {}, dispatchQueued() {},
    async saveDirectorDraft(id, payload) { requests.push({ id, payload }); return save(id, payload) },
    async refresh() {}, execute(text) { newInputs.push(text) },
  }
  const api = new Function('state', `const {${Object.keys(state).filter(key => key !== 'epoch').join(',')}}=state; let nextInputId=0; ${js}; return {reviseByInstruction,saveFinal,sendInput,invalidate}`)(state)
  return { state, requests, filled, newInputs, ...api }
}

test('explicit instruction editing targets one plan, then normal chat creates an independent plan', async () => {
  const view = revisionWorkspace()
  view.reviseByInstruction()
  assert.equal(view.state.revisionVersion.value, 1)
  assert.equal(view.state.chatExpanded.value, true)
  await view.saveFinal('她满怀爱意看着镜头')
  assert.deepEqual(view.requests, [{ id: 'plan-a', payload: {
    expected_project_version: 2, expected_director_version: 1, conversation_id: 'owner',
    revision_instruction: '她满怀爱意看着镜头',
  } }])
  assert.equal(view.state.selectedRun.value, 'saved-a')
  assert.equal(view.state.revisionVersion.value, null)
  assert.equal(view.state.revisionTarget.value, null)
  assert.ok(view.state.drafts.value['other-plan'])
  view.sendInput('她坐在窗边')
  assert.deepEqual(view.newInputs, ['她坐在窗边'])
  assert.equal(view.requests.length, 1)
})

test('direct editing saves the selected draft without invoking instruction revision', async () => {
  const view = revisionWorkspace()
  view.state.fields.value = { 'director_plan.composition_strategy': '新的手动构图' }
  view.state.drafts.value['a-v1'] = view.state.fields.value
  view.state.dirty.value = true
  view.reviseByInstruction()
  assert.equal(view.state.revisionVersion.value, null)
  await view.saveFinal()
  assert.equal(view.requests[0].payload.draft.director_plan.composition_strategy, '新的手动构图')
  assert.equal('revision_instruction' in view.requests[0].payload, false)
  assert.ok(view.state.drafts.value['other-plan'])
  assert.equal(view.state.drafts.value['a-v1'], undefined)
})

test('changed revision target is rejected instead of silently modifying the new selection', async () => {
  for (const change of [
    state => { state.project.value.project.project_id = 'plan-b' },
    state => { state.selectedRun.value = 'run-b' },
    state => { state.spec.value.version = 2 },
    state => { state.activeConversationId.value = 'another-owner' },
    state => { state.dirty.value = true },
  ]) {
    const view = revisionWorkspace()
    view.reviseByInstruction(); change(view.state)
    await view.saveFinal('修改火焰质感')
    assert.deepEqual(view.requests, [])
    assert.deepEqual(view.filled, ['修改火焰质感'])
    assert.equal(view.state.revisionVersion.value, null)
    assert.match(view.state.error.value, /目标已变化/)
  }
})

test('failed instruction preserves its input and pinned versions for retry, never becomes new chat', async () => {
  const view = revisionWorkspace(async () => { throw new Error('模型回复无效') })
  view.reviseByInstruction()
  view.state.project.value.project.current_version = 3
  await view.saveFinal('保留人物信息，调整表情')
  assert.equal(view.requests[0].payload.expected_project_version, 2)
  assert.equal(view.state.revisionVersion.value, 1)
  assert.equal(view.state.selectedRun.value, 'run-a')
  assert.equal(view.state.restoredSpec.value, null)
  assert.deepEqual(view.filled, ['保留人物信息，调整表情'])
  assert.equal(view.state.busy.value, false)
  view.state.busy.value = true
  view.sendInput('忙碌期间的修改')
  assert.deepEqual(view.filled, ['保留人物信息，调整表情', '忙碌期间的修改'])
  assert.equal(view.state.queuedInputs.value.length, 0)
  assert.deepEqual(view.newInputs, [])
})

test('late revision response never replaces another plan or conversation', async () => {
  for (const change of [
    state => { state.selectedRun.value = 'run-b'; state.project.value.project.project_id = 'plan-b' },
    state => { state.epoch++; state.activeConversationId.value = 'other' },
  ]) {
    let release
    const view = revisionWorkspace(() => new Promise(resolve => { release = resolve }))
    view.reviseByInstruction()
    const pending = view.saveFinal('调整构图')
    change(view.state)
    release({ project_id: 'plan-a', version: 2 })
    await pending
    assert.equal(view.requests[0].id, 'plan-a')
    assert.equal(view.requests[0].payload.conversation_id, 'owner')
    assert.equal(view.state.restoredSpec.value, null)
  }
})

test('current unavailable review cannot be replaced by an older Run finding', async () => {
  const html = await renderNode({ stage: 'director_critic', mode: 'professional',
    spec: { ...spec, critic_result: null, critic_feedback: [], critic_status: 'unavailable' },
    critic: { verdict: 'blocked', public_summary: '旧方案问题', findings: [{
      code: 'EXECUTION_DECISION_UNRESOLVED', evidence: '旧方案机位错误', suggested_action: '旧建议',
    }] },
  })
  assert.match(html, /暂无真实审核结果/)
  assert.doesNotMatch(html, /旧方案|旧建议|EXECUTION_DECISION_UNRESOLVED|审核通过/)
})

test('Director navigation can reopen saved plan after a followup fails without hiding its history', () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const start = view.indexOf('function visitStage(')
  const js = ts.transpileModule(view.slice(start, view.indexOf('\nfunction scrollState(', start)),
    { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const ref = value => ({ value })
  const executions = [
    { run_id: 'failed-followup', director_spec: null },
    { run_id: 'current-director', director_spec: { version: 2 } },
    { run_id: 'old-director', director_spec: { version: 1 } },
  ]
  const env = { spec: ref(null), busy: ref(false), hasRunning: ref(false),
    executions: ref(executions), workExecutions: ref(executions), project: ref({ project: { director_version: 2 } }),
    selectedRun: ref('failed-followup'), section: ref('conversation'), selectedStage: ref('director_assemble'),
    chatExpanded: ref(true), inspectorOpen: ref(false) }
  const open = new Function('env', `const {${Object.keys(env).join(',')}}=env;
    const toInternalSection = id => id === 'plan' ? 'director' : id;
    ${js}; return openSection`)(env)
  open('plan')
  assert.equal(env.selectedRun.value, 'current-director')
  assert.equal(env.chatExpanded.value, false)
  assert.equal(env.section.value, 'director')
  assert.equal(executions[0].run_id, 'failed-followup')
  // 已被旧页面分叉的历史仍可只读打开，不能把它重新确认成当前方案。
  env.project.value.project.director_version = null
  env.selectedRun.value = 'failed-followup'
  open('plan')
  assert.equal(env.selectedRun.value, 'current-director')
  assert.equal(canConfirmDirector('completed', { ...spec, creative_brief_version: 1 }, true, false), false)
  env.busy.value = true
  env.selectedRun.value = 'running-followup'
  open('plan')
  assert.equal(env.selectedRun.value, 'running-followup')
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
  assert.match(workspace, /navigate\(\{ \.\.\.route.value, workspacePage, directorStage, directorRunId, workId, taskId,/)
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

test('new workspace submits a director draft and preserves mandatory user consent', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf('async function execute('), view.indexOf('\nfunction sendInput('))
  const code = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const ref = value => ({ value })
  const submissions = []
  const state = {
    busy: ref(false), hasRunning: ref(false), project: ref(null), composer: ref(null),
    draftKey: ref('draft'), mode: ref('professional'), activeConversationId: ref('current'),
    manualDirectorApproval: ref(true), pendingText: ref(''), pendingRequestId: ref(''), previousRunIds: ref([]),
    executions: ref([]), error: ref(''), restoredSpec: ref(null), selectedRun: ref(''), drafts: ref({}),
    runs: ref({}), directorConversationExecutions, submittingDirector: ref(false),
    async getRun() { return { id: 'director', state: { project_id: 'project' } } },
    invalidate() {}, failureText: String, async refresh() {}, loadConversations() {},
    async createComicProject() { return { project: { project_id: 'project', current_version: 1 } } },
    async createDirectorExecution(id, payload) { submissions.push({ id, payload }); return { run_id: 'director' } },
    createRun() { throw new Error('external workspace cannot start image production') },
  }
  const execute = new Function('state', `const {${Object.keys(state).join(',')}}=state; const conversationEpoch=1,workSelectionEpoch=0,disposed=false,activeWork={value:null}; ${code}; return execute`)(state)
  await execute('少女竹林')
  assert.equal(submissions.length, 1)
  assert.equal(submissions[0].payload.creation_mode, 'professional')
  assert.equal(submissions[0].payload.conversation_id, 'current')
  assert.equal(state.selectedRun.value, 'director')
  assert.match(view, /v-if="manualDirectorApproval && spec && selectedStage === 'director_assemble'"/)
  assert.match(view, /getRun\(productionRun.value.id\)/)
  assert.match(view, /getEvents\(productionRun.value.id\)/)
  assert.match(view, /turn.artifactId && referenceUrls\[turn.artifactId\]/)
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
  const state = { confirmed: ref(false), productionEligible: ref(true), busy: ref(false), hasRunning: ref(false),
    error: ref(''), project: ref({ project: { project_id: 'project', current_version: 2 }, creative_brief: { original_request: '少女竹林' } }),
    spec: ref({ version: 1 }), boards: ref([]), selectedBoard: ref(''), selectedShot: ref('') }
  const actions = []
  const enter = new Function('state', 'actions', `const {${Object.keys(state).join(',')}}=state;
    const conversationEpoch=1,workSelectionEpoch=0; function openSection(value){actions.push(value)};
    async function loadPage(){}; async function refresh(){}; function failureText(e){return String(e)};
    async function createComicStoryboard(id,version,task){actions.push(['plan',id,version,task]);return {storyboard:{storyboard_id:'board'},shots:[{shot_id:'shot'}]}};
    async function generateImage(){throw new Error('Entering storyboard must not generate images')};
    ${code}; return enterStoryboard`)(state, actions)
  await enter()
  assert.deepEqual(actions, [])
  state.confirmed.value = true
  await enter()
  assert.deepEqual(actions, ['storyboard', ['plan', 'project', 2, '少女竹林']])
  assert.equal(state.selectedShot.value, 'shot')
  assert.match(view, /section === 'storyboard' && productionEligible/)
  assert.doesNotMatch(view, /async function generateImage|createRun\(/)
})

test('external workspace preserves manual director consent regardless of director depth', () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  assert.match(view, /manualDirectorApproval = ref\(true\)/)
  assert.doesNotMatch(view, /manualDirectorApproval.value = false|v-model="manualDirectorApproval"/)
  assert.match(view, /导演方案需人工确认 · 外部图片/)
  assert.doesNotMatch(view, /处理制作审批|name: 'tasks'/)
  assert.match(view, /quickDirectorMessage\(item.content\)/)
})


test('regenerate is explicit new generation; unknown recovery only resumes the same run', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const source = view.slice(view.indexOf('async function recoverImage'), view.indexOf('function editImagePrompt'))
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const ref = value => ({ value })
  const actions = []
  const state = { imageExecution: ref({ can_regenerate: false }), productionRun: ref({ id: 'original' }),
    busy: ref(false), hasRunning: ref(false), error: ref(''), productionEligible: ref(true) }
  const result = new Function('state', 'actions', `const {${Object.keys(state).join(',')}}=state;
    const conversationEpoch=1,workSelectionEpoch=0; async function generateImage(isNew){actions.push(['new',isNew])};
    async function resumeRun(id){actions.push(['resume',id]);return {id}};
    async function refresh(){}; function failureText(e){return String(e)}; ${code};
    return { recoverImage }`)(state, actions)
  await result.recoverImage()
  assert.deepEqual(actions, [['resume', 'original']])
  await result.recoverImage('shot-original')
  assert.deepEqual(actions, [['resume', 'original'], ['resume', 'shot-original']])
  assert.doesNotMatch(view, /async function generateImage|productionRequests/)
  assert.match(view, /保存为新版本/)
})


test('persisted user messages retain both derived replies, ordered after each input', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const { mergeChatTurnsByTime } = await import(presentationUrl)
  const start = view.indexOf('const chatTurns = computed')
  const body = view.slice(start, view.indexOf('async function loadConversations', start))
  const code = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const environment = {
    computed: fn => fn(), mergeChatTurnsByTime, quickDirectorMessage: text => text,
    conversationMessages: { value: [1, 2].map(i => ({ id: `user${i}`, run_id: `run${i}`, role: 'user', content: `question${i}`, created_at: `2026-10-07T10:0${i}:00.900Z` })) },
    runs: { value: Object.fromEntries([1, 2].map(i => [`run${i}`, { started_at: `2026-10-07T10:0${i}:00.100Z`, state: {} }])) },
    transcript: { value: [1, 2].map(i => ({ execution: { run_id: `run${i}`, status: 'completed' }, text: `question${i}`, answer: `answer${i}` })) },
    pendingText: { value: '' }, pendingRequestId: { value: '' }, previousRunIds: { value: ['run1', 'run2'] },
  }
  const turns = new Function('env', `const {${Object.keys(environment).join(',')}}=env; ${code}; return chatTurns`)(environment)
  assert.deepEqual(turns.map(turn => turn.content), ['question1', 'answer1', 'question2', 'answer2'])
  assert.deepEqual(turns.filter(turn => turn.role === 'assistant').map(turn => turn.runId), ['run1', 'run2'])
  assert.match(view, /@click="openDraft\(turn.runId\)"/)
  environment.pendingText.value = 'question1'
  const repeated = new Function('env', `const {${Object.keys(environment).join(',')}}=env; ${code}; return chatTurns`)(environment)
  assert.equal(repeated.at(-1).id, 'pending-user')
})

test('thinking, message persistence and Run binding show one bubble for each request', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const { mergeChatTurnsByTime } = await import(presentationUrl)
  const start = view.indexOf('const chatTurns = computed')
  const code = ts.transpileModule(view.slice(start, view.indexOf('async function loadConversations', start)),
    { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const environment = {
    computed: fn => fn(), mergeChatTurnsByTime, quickDirectorMessage: text => text,
    conversationMessages: ref([{ id: 'old-failure', role: 'user', content: '相同创意',
      run_id: null, event_id: 'director-request:old', created_at: '2026-10-12T01:00:00Z' }]),
    runs: ref({}), transcript: ref([]), pendingText: ref('相同创意'),
    pendingRequestId: ref('new-request'), previousRunIds: ref([]),
  }
  const readTurns = new Function('env', `const {${Object.keys(environment).join(',')}}=env; ${code}; return chatTurns`)
  const users = () => readTurns(environment).filter(turn => turn.role === 'user')
  assert.deepEqual(users().map(turn => turn.id), ['old-failure', 'pending-user'])
  environment.conversationMessages.value.push({ id: 'new-user', role: 'user', content: '相同创意',
    run_id: null, event_id: 'director-request:new-request', created_at: '2026-10-12T02:00:00Z' })
  assert.deepEqual(users().map(turn => turn.id), ['old-failure', 'new-user'])
  environment.runs.value.new = { started_at: '2026-10-12T02:00:01Z',
    state: { creative_context: { input_message_id: 'new-user' } } }
  environment.transcript.value = [{ execution: { run_id: 'new', status: 'running' },
    text: '相同创意', label: '正在思考' }]
  assert.deepEqual(users().map(turn => turn.id), ['old-failure', 'new-user'])
  assert.equal(readTurns(environment).filter(turn => turn.role === 'assistant').length, 1)
  environment.conversationMessages.value[1].run_id = 'new'
  environment.pendingText.value = ''
  environment.transcript.value[0] = { execution: { run_id: 'new', status: 'completed' },
    text: '相同创意', answer: '导演草稿已保存' }
  assert.deepEqual(users().map(turn => turn.id), ['old-failure', 'new-user'])
  assert.equal(readTurns(environment).at(-1).content, '导演草稿已保存')
  environment.runs.value.new.state.generation_incomplete = true
  environment.conversationMessages.value.push({ id: 'old-answer', role: 'assistant', type: 'plan',
    content: '当前画面方案已保存，可以继续讨论或修改。', run_id: 'new', created_at: '2026-10-12T02:00:02Z' })
  assert.match(readTurns(environment).at(-1).content, /系统内部检查尚未完成/)
  assert.equal(environment.conversationMessages.value.at(-1).content, '当前画面方案已保存，可以继续讨论或修改。')
})

/* 交付页（UX-011）：当前镜头图片必须是第一优先，且下载入口直接可用；
   能力边界（已上传 ≠ 视觉质检通过 ≠ 视频完成）必须写明。
   这里渲染真实的 ChatImageAttachment 组件，避免只断言源码字符串。 */
const attachSource = readFileSync(new URL('../src/components/chat/ChatImageAttachment.vue', import.meta.url), 'utf8')
const attachScript = compileScript(parse(attachSource).descriptor, { id: 'delivery-attachment', inlineTemplate: true }).content
const attachModule = ts.transpileModule(attachScript, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  .replace(/from (["'])vue\1/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`)
  .replace(/from (["'])lucide-vue-next\1/g, `from ${JSON.stringify(import.meta.resolve('lucide-vue-next'))}`)
const { default: ChatImageAttachment } = await import(`data:text/javascript;base64,${Buffer.from(attachModule).toString('base64')}`)

test('delivery page leads with the current shot image and exposes a direct download', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  // 交付区必须排在资产分组之前，空分组不得占据页面开头。
  const heroAt = view.indexOf('class="delivery-hero"')
  const groupAt = view.indexOf('class="asset-group collapsible"')
  assert.ok(heroAt > 0 && groupAt > heroAt, '当前镜头交付区必须先于可折叠资产分组出现')
  // 下载入口：真实 <a download>，不是"查看"。
  assert.match(view, /download="kantoku-shot\.png"/)
  assert.match(view, /下载当前镜头图片/)
  // 能力边界必须显式写明，不能让"图片已上传"冒充质检通过或视频完成。
  assert.match(view, /不代表视觉质检已通过，也不代表视频已生成/)
  assert.match(view, /video|视频/)
  // 空分组收缩为一行：使用 <details> 且默认按有无内容决定展开。
  assert.match(view, /<details v-for="\(label, kind\) in \{ character: '角色资产'/)
  assert.match(view, /:open="assets\.some\(item => item\.details\.kind === kind && item\.state !== 'deleted'\)"/)

  const html = await renderToString(createSSRApp({ render: () => h(ChatImageAttachment, {
    media: { url: 'blob:kantoku-shot', filename: 'kantoku-shot.png' },
  }) }))
  assert.match(html, /download="kantoku-shot\.png"/)
  assert.match(html, /下载原图/)
})

/* 提示词历史恢复（UX-007）：与导演层同范式——二次确认 + 历史保留 + 确认态清除 +
   来源过期前置阻塞原因。 */
test('prompt history exposes restore-as-new-version with a guarded confirmation', () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  // 只有非当前版本才能恢复；当前版本提供的是"编辑为新版本"。
  assert.match(view, /v-else class="ui-button sm" :disabled="busy \|\| hasRunning" @click="promptRestoreChoice = Number\(prompt\.version\)">恢复为新版本/)
  // 确认区文案必须说明：历史保留 + 确认状态清除 + 需重新确认。
  assert.match(view, /历史版本全部保留，当前确认状态会清除，恢复后需重新确认/)
  // 来源过期时按钮禁用，并给出真实原因。
  assert.match(view, /:disabled="busy \|\| hasRunning \|\| !!stalePromptSource\(prompt\)"/)
  assert.match(view, /function stalePromptSource/)
  assert.match(view, /请重新编译生成新版本/)
  // 走既有后端接口，不新增路由。
  assert.match(view, /restoreComicPrompt\(shotId, version, Number\(current\.version\), project\.value\.project\.current_version\)/)
})

/* 刷新后可编辑（UX-010）：以作品当前稿为权威来源，并可一键从历史切回当前。 */
test('restored director plan stays editable after refresh through the saved current version', () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  assert.match(view, /async function syncCurrentDirector/)
  assert.match(view, /await getCurrentDirector\(projectId\)/)
  // 恢复动作后立即同步权威稿 —— 这是刷新后仍可编辑的关键。
  assert.match(view, /restoredSpec\.value = restored[\s\S]{0,400}await syncCurrentDirector\(\)/)
  // 历史页给出回当前稿的唯一入口，且不产生新版本。
  assert.match(view, /查看当前版本/)
  assert.match(view, /function viewCurrentDirector[\s\S]{0,200}await syncCurrentDirector\(\)\s*\n\s*restoredSpec\.value = currentDirector\.value/)
  // 选择历史消息必须展示它自己的版本；新版只能经明确的“查看当前版本”进入。
  const selection = view.slice(view.indexOf('const spec = computed'), view.indexOf('const node = computed'))
  assert.doesNotMatch(selection, /currentDirectorIsNewer/)
  // 循环类型推断已排除：比较对象是 active 的执行记录，不是 spec。
  assert.match(view, /const mine = Number\(\(active\.value\?\.director_spec as \{ version\?: number \} \| undefined\)\?\.version\)/)
})

/* 刷新不回退（UX-006）：恢复现场时不得覆盖 URL 的 workspacePage。 */
test('opening an existing conversation during restore never rewrites the requested page', () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  assert.match(view, /async function openConversation\(id: string, initialDirectorRun = '', options: \{ restore\?: boolean \} = \{\}\)/)
  assert.match(view, /const restoring = !!requestedPage && requestedPage !== 'conversation'/)
  assert.match(view, /openConversation\(id, requestedRunId, \{ restore: restoring \}\)/)
  // restore 分支不得写入 workspacePage:'conversation'
  const fn = view.slice(view.indexOf('async function openConversation('), view.indexOf('async function finishConversationRename('))
  const restoreBranch = fn.slice(fn.indexOf('if (restore) {'), fn.indexOf('} else {'))
  assert.doesNotMatch(restoreBranch, /workspacePage: 'conversation'/)
  assert.match(fn.slice(fn.indexOf('} else {')), /workspacePage: 'conversation'/)
})


test('late Prompt results and failures cannot replace a different work in the same chat', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf('async function compilePrompt('), view.indexOf('async function viewImagePrompt('))
  const code = ts.transpileModule(body.replace(/\bworkSelectionEpoch\b/g, 'state.workEpoch'), { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  for (const fails of [false, true]) {
    let finish, fail
    const state = { workEpoch: 1, shots: ref([{ shot_id: 'old-shot', version: 1 }]),
      boards: ref([{ storyboard_id: 'old-board', director_spec_version: 1 }]),
      selectedShot: ref('old-shot'), selectedBoard: ref('old-board'),
      project: ref({ project: { project_id: 'shared-legacy-project', current_version: 8 } }),
      manualDirectorApproval: ref(true), confirmed: ref(true), spec: ref({ version: 1 }),
      compiling: ref(false), busy: ref(false), hasRunning: ref(false), activeConversationId: ref('same-chat'),
      preparingShot: ref(''), error: ref(''), shotErrors: ref({}), shotPrompts: ref({}), promptPreview: ref(null),
      async compileComicPrompt() { return new Promise((resolve, reject) => { finish = resolve; fail = reject }) },
      async refresh() { throw new Error('stale response must not refresh the current work') },
      async loadPrompts() { throw new Error('stale response must not load another work') }, failureText: String }
    const compile = new Function('state', `const {${Object.keys(state).filter(key => key !== 'workEpoch').join(',')}}=state; const conversationEpoch=1; ${code}; return compilePrompt`)(state)
    const pending = compile()
    state.workEpoch++
    state.selectedShot.value = 'current-shot'
    // Selecting another work clears the old loader before its new request begins.
    state.compiling.value = true
    state.preparingShot.value = 'current-shot'
    state.promptPreview.value = { artifact_id: 'current-work-prompt' }
    if (fails) fail(new Error('old work failure'))
    else finish({ artifact_id: 'old-work-prompt' })
    await pending
    assert.equal(state.promptPreview.value.artifact_id, 'current-work-prompt')
    assert.deepEqual(state.shotPrompts.value, {})
    assert.equal(state.error.value, '')
    assert.equal(state.compiling.value, true)
    assert.equal(state.preparingShot.value, 'current-shot')
  }
})


test('a late director result keeps its original request owner and cannot switch the selected work', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf('async function execute('), view.indexOf('function sendInput('))
  const code = ts.transpileModule(body.replace(/\bworkSelectionEpoch\b/g, 'state.workEpoch'), { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  let finish
  const sent = []
  const state = { workEpoch: 1, busy: ref(false), hasRunning: ref(false),
    project: ref({ project: { project_id: 'source-project', current_version: 5 } }),
    composer: ref(null), draftKey: ref('old-draft'), activeConversationId: ref('chat'), mode: ref('professional'),
    pendingRequestId: ref(''), pendingText: ref(''), previousRunIds: ref([]), executions: ref([]), error: ref(''),
    submittingDirector: ref(false), restoredSpec: ref(null), selectedRun: ref('source-run'), invalidate() {},
    async getComicProject(id) { return { project: { project_id: id, current_version: 5 } } },
    async createDirectorExecution(id, payload) {
      sent.push({ id, payload }); return new Promise(resolve => { finish = resolve })
    }, failureText: String }
  const execute = new Function('state', `const {${Object.keys(state).filter(key => key !== 'workEpoch').join(',')}}=state; const conversationEpoch=1,disposed=false,activeWork={value:null}; ${code}; return execute`)(state)
  const pending = execute('独立新创意')
  await new Promise(resolve => setImmediate(resolve))
  state.workEpoch++
  state.project.value = { project: { project_id: 'selected-project', current_version: 9 } }
  state.selectedRun.value = 'selected-run'
  state.restoredSpec.value = { spec_id: 'selected-spec' }
  finish({ run_id: 'late-director', director_spec: { project_id: 'new-result-project' } })
  await pending
  assert.equal(sent.length, 1)
  assert.equal(sent[0].id, 'source-project')
  assert.equal(sent[0].payload.conversation_id, 'chat')
  assert.equal(state.project.value.project.project_id, 'selected-project')
  assert.equal(state.selectedRun.value, 'selected-run')
  assert.equal(state.restoredSpec.value.spec_id, 'selected-spec')
  assert.equal(state.busy.value, false)
  assert.equal(state.pendingText.value, '')
})

test('work dialogue submission pins source identity and late results cannot replace another task', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf('async function executeTaskTurn('), view.indexOf('\nfunction sendInput('))
  const code = ts.transpileModule(body.replace(/\bconversationEpoch\b/g, 'state.epoch').replace(/\bworkSelectionEpoch\b/g, 'state.workEpoch'), { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  let release
  const sent = [], saved = new Map()
  const state = { epoch: 1, workEpoch: 1, busy: ref(false), conversationBusy: ref(false), hasRunning: ref(false), pendingTaskRequest: ref(''),
    activeWork: ref({ work_id: 'work-a' }), activeTask: ref({ task_id: 'task-a', project_id: 'task-a', conversation_id: 'chat-a' }),
    pendingRequestId: ref(''), submittingDirector: ref(false), pendingText: ref(''), error: ref(''), executions: ref([]), previousRunIds: ref([]),
    selectedRun: ref('selected-b'), restoredSpec: ref(null), runs: ref({}), composer: ref(null), failureText: String,
    async getComicProject(id) { return { project: { project_id: id, current_version: 3, director_version: 1 } } },
    async getComicWork() { return { work_id: 'work-a', version: 7, conversation_id: 'work-chat-a' } },
    async getComicWorkbench() { return { state: { image: { image_id: 'image-a', version: 2 } } } },
    async sendComicWorkTurn(...args) { sent.push(args); return new Promise(resolve => { release = resolve }) },
    rememberTaskRequest(taskId, id) { saved.set(taskId, id) },
    refresh() { throw new Error('late response must not refresh the selected task') },
  }
  const execute = new Function('state', `const {${Object.keys(state).filter(key => !['epoch','workEpoch'].includes(key)).join(',')}}=state; const disposed=false; ${code}; return executeTaskTurn`)(state)
  const pending = execute('修改当前画面的光线', 'professional')
  await new Promise(resolve => setImmediate(resolve))
  state.epoch++; state.workEpoch++
  state.activeWork.value = { work_id: 'work-b' }
  state.activeTask.value = { task_id: 'task-b', conversation_id: 'chat-b' }
  state.pendingText.value = ''; state.busy.value = false
  release({ run_id: 'late-a', director_spec: { project_id: 'task-a' } })
  await pending
  assert.equal(sent[0][0], 'work-a')
  assert.equal(sent[0][1].conversation_id, 'work-chat-a')
  assert.equal(sent[0][1].target_task_id, 'task-a')
  assert.equal(sent[0][1].independent_plan, true)
  assert.equal(sent[0][1].expected_project_version, 7)
  assert.deepEqual(sent[0][1].turn_image, { image_id: 'image-a', version: 2 })
  assert.equal(state.selectedRun.value, 'selected-b')
  assert.equal(state.activeTask.value.task_id, 'task-b')
  assert.equal(saved.get('work-a'), '')
})

test('opening legacy history clears modern ownership and preserves explicit source links', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf('async function openConversation('), view.indexOf('\nasync function finishConversationRename('))
  const code = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const state = { workspaceWorks: ref([]), activeWork: ref({ work_id: 'modern-work' }), activeTask: ref({ task_id: 'modern-task' }),
    activeConversationId: ref('modern-chat'), loading: ref(false), error: ref(''), chatExpanded: ref(false), inspectorOpen: ref(false),
    storyWorkbench: { dirty: ref(false) }, route: ref({ workId: 'modern-work', taskId: 'modern-task', workspacePage: 'assets' }),
    localStorage: { setItem() {} }, resetConversationView() {}, failureText: String,
    async getConversation() { return null }, async getComicConversationWorks() { return [] },
    navigate(target) { assert.equal(state.loading.value, true); state.route.value = target },
  }
  const open = new Function('state', `const {${Object.keys(state).join(',')}}=state; let conversationEpoch=0; const disposed=false; ${code}; return openConversation`)(state)
  await open('legacy-chat')
  assert.equal(state.activeWork.value, null)
  assert.equal(state.activeTask.value, null)
  assert.equal(state.route.value.workId, undefined)
  assert.equal(state.route.value.taskId, undefined)
  assert.equal(state.route.value.conversationId, 'legacy-chat')
  state.workspaceWorks.value = [{ work_id: 'modern-work', tasks: [{ task_id: 'source-task', conversation_id: 'legacy-chat' }] }]
  state.route.value = { workId: 'source-project:brief:1', workspacePage: 'plan', directorRunId: 'original-run' }
  await open('legacy-chat', 'original-run', { restore: true })
  assert.equal(state.activeWork.value, null)
  assert.equal(state.route.value.workId, 'source-project:brief:1')
  assert.equal(state.route.value.directorRunId, 'original-run')
})

test('an exact legacy source Run wins over the latest version in its Brief group', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf('async function openConversation('), view.indexOf('\nasync function finishConversationRename('))
  const code = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const records = ['first', 'latest'].map(id => ({ id, workflow: 'comic.director', status: 'completed',
    state: { project_id: 'source', conversation_id: 'chat' } }))
  const state = { computed, workspaceWorks: ref([]), activeWork: ref(null), activeTask: ref(null),
    activeConversationId: ref(''), loading: ref(false), error: ref(''), chatExpanded: ref(false), inspectorOpen: ref(false),
    storyWorkbench: { dirty: ref(false) }, route: ref({ workId: 'source:brief:1', workspacePage: 'plan', directorRunId: 'first' }),
    localStorage: { setItem() {} }, resetConversationView() {}, failureText: String,
    conversationMessages: ref([]), works: ref([]), selectedWorkId: ref(''), runs: ref({}), productionRun: ref(null),
    selectedRun: ref(''), project: ref(null), executions: ref(records.map(run => ({ run_id: run.id }))),
    active: ref({ director_execution_summary: { mode: 'professional' } }), mode: ref('professional'), assets: ref([]),
    currentDirector: ref(null), composer: ref(null), nextTick, conversationWorkspaceRun,
    ownsConversationRun: (run, owner) => run.state.conversation_id === owner,
    async getRun(id) { return records.find(run => run.id === id) },
    async getConversation() { return { messages: [], related_run_ids: ['first', 'latest'] } },
    async getComicConversationWorks() { return [{ work_id: 'source:brief:1', project_id: 'source', has_director: true, director_run_id: 'latest' }] },
    async getComicProject() { return { project: { project_id: 'source', director_version: 5 } } },
    async refresh() {}, async syncCurrentDirector() {},
    navigate(target) { state.route.value = target },
  }
  const open = new Function('state', `const {${Object.keys(state).join(',')}}=state; let conversationEpoch=0; const disposed=false; ${code}; return openConversation`)(state)
  await open('chat', 'first', { restore: true })
  assert.equal(state.error.value, '')
  assert.equal(state.selectedRun.value, 'first')
  assert.equal(state.route.value.directorRunId, 'first')
})

function workspaceFunction(name, next, state, locals = '') {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf(`async function ${name}(`), view.indexOf(next, view.indexOf(`async function ${name}(`)))
    .replace(/\bconversationEpoch\b/g, 'state.epoch').replace(/\bworkSelectionEpoch\b/g, 'state.workEpoch')
  const code = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText
  return new Function('state', `const {${Object.keys(state).filter(key => !['epoch', 'workEpoch'].includes(key)).join(',')}}=state; const disposed=false; ${locals}; ${code}; return ${name}`)(state)
}
const flushWorkspace = () => new Promise(resolve => setImmediate(resolve))
function deferredWorkspace() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test('returning before the first task exists restores the pending request and polls without submitting again', async () => {
  const work = { work_id: 'work', conversation_id: 'chat', tasks: [] }
  const state = { epoch: 0, workEpoch: 0, activeWork: ref(null), activeTask: ref(null), activeConversationId: ref(''),
    storyWorkbench: { dirty: ref(false) }, route: ref({ workspacePage: 'conversation' }),
    loading: ref(true), chatExpanded: ref(false), section: ref(''), pendingTaskRequest: ref(''), busy: ref(false),
    error: ref(''), refreshConnectionError: ref(''), project: ref(null), failureText: String,
    sessionStorage: { getItem(key) { return key.endsWith(':work') ? 'original-request' : null } },
    resetConversationView() {}, toInternalSection: value => value, navigate() {}, async loadConversationMessages() {} }
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const restoreCode = ts.transpileModule(view.slice(view.indexOf('function restoreTaskRequest('),
    view.indexOf('\nasync function recoverTaskRequest(')), { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  state.restoreTaskRequest = new Function('pendingTaskRequest', 'sessionStorage', `${restoreCode}; return restoreTaskRequest`)(state.pendingTaskRequest, state.sessionStorage)
  const open = workspaceFunction('openWorkspace', '\nasync function openImageTask(', state)
  await open(work, '', true)
  assert.equal(state.project.value, null)
  assert.equal(state.pendingTaskRequest.value, 'original-request')
  let queries = 0, refreshes = 0
  state.recoverTaskRequest = async () => { queries++ }
  state.refresh = async () => { refreshes++ }
  const poll = workspaceFunction('pollOnce', '\nfunction scheduleRefresh(', state)
  await poll(); await poll()
  assert.equal(queries, 2)
  assert.equal(refreshes, 0)
  assert.equal(state.error.value, '')
})

test('original request recovery waits through running or unknown status and applies the newly created task once', async () => {
  const response = deferredWorkspace(), opened = [], messages = []
  const work = { work_id: 'work', tasks: [] }
  let reads = 0, status = { phase: 'submitted', result: null }
  const state = { epoch: 1, workEpoch: 1, activeWork: ref(work), activeTask: ref(null), activeConversationId: ref('chat'),
    pendingTaskRequest: ref('request'), pendingText: ref('原输入'), busy: ref(false), error: ref(''),
    refreshConnectionError: ref(''), project: ref(null), failureText: String,
    getComicWorkTurn() { reads++; return reads === 1 ? response.promise : status },
    async loadConversationMessages(id) { messages.push(id) },
    rememberTaskRequest(_id, value) { state.pendingTaskRequest.value = value },
    async getComicWork() { return { ...work, tasks: [{ task_id: 'new-task' }] } },
    async openImageTask(...args) { opened.push(args); state.epoch++; state.workEpoch++;
      state.activeTask.value = { task_id: args[1] }; state.project.value = { project: { project_id: args[1] } } },
    sendComicWorkTurn() { throw new Error('recovery must never submit a model request') } }
  const recover = workspaceFunction('recoverTaskRequest', '\nconst previousRunIds =', state, 'let recoveringTaskRequest=null')
  const first = recover(), duplicate = recover()
  await flushWorkspace(); assert.equal(reads, 1)
  response.resolve(status); await Promise.all([first, duplicate])
  status = { phase: 'running', result: null }; await recover()
  status = { phase: null, result: null }; await recover()
  status = { phase: 'unknown', result: null }; await recover()
  assert.equal(state.error.value, '')
  assert.equal(state.pendingTaskRequest.value, 'request')
  assert.equal(opened.length, 0)
  status = { phase: 'completed', result: { task_id: 'new-task', run_id: 'saved-run' } }
  await recover(); await recover()
  assert.equal(opened.length, 1)
  assert.deepEqual(opened[0].slice(0, 4), ['work', 'new-task', true, 'saved-run'])
  assert.equal(state.pendingTaskRequest.value, '')
  assert.equal(state.pendingText.value, '')
  assert.equal(state.refreshConnectionError.value, '')
  assert.equal(messages.every(id => id === 'chat'), true)
})

test('request reads cannot reopen another selection in the same work and query errors remain retryable', async () => {
  const response = deferredWorkspace(), opened = []
  const state = { epoch: 1, workEpoch: 1, activeWork: ref({ work_id: 'work' }), activeTask: ref(null),
    activeConversationId: ref('chat'), pendingTaskRequest: ref('request'), busy: ref(false),
    error: ref(''), refreshConnectionError: ref(''), failureText: String,
    getComicWorkTurn() { return response.promise }, openImageTask(...args) { opened.push(args) } }
  const recover = workspaceFunction('recoverTaskRequest', '\nconst previousRunIds =', state, 'let recoveringTaskRequest=null')
  const old = recover(); await flushWorkspace()
  state.epoch++; state.workEpoch++
  response.resolve({ phase: 'completed', result: { task_id: 'old-task' } }); await old
  assert.equal(opened.length, 0)
  assert.equal(state.pendingTaskRequest.value, 'request')
  state.getComicWorkTurn = async () => { throw new Error('offline query failed') }
  const retry = workspaceFunction('recoverTaskRequest', '\nconst previousRunIds =', state, 'let recoveringTaskRequest=null')
  await retry()
  assert.match(state.refreshConnectionError.value, /offline query failed/)
  assert.equal(state.pendingTaskRequest.value, 'request')
})

test('late completion can clear only its own request marker, preserving the next turn', () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = ts.transpileModule(view.slice(view.indexOf('function rememberTaskRequest('),
    view.indexOf('\nfunction restoreTaskRequest(')), { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const saved = new Map([['kantoku-comic-task-request:work', 'new-request']])
  const state = { activeWork: ref({ work_id: 'work' }), activeTask: ref(null), pendingTaskRequest: ref('new-request'),
    sessionStorage: { getItem: key => saved.get(key), removeItem: key => saved.delete(key) } }
  const remember = new Function('state', `const {${Object.keys(state).join(',')}}=state; ${body}; return rememberTaskRequest`)(state)
  remember('work', '', 'old-request')
  assert.equal(state.pendingTaskRequest.value, 'new-request')
  assert.equal(saved.get('kantoku-comic-task-request:work'), 'new-request')
  remember('work', '', 'new-request')
  assert.equal(state.pendingTaskRequest.value, '')
  assert.equal(saved.has('kantoku-comic-task-request:work'), false)
})

test('a finished discussion restores its messages even when the work has no image task', async () => {
  let messages = 0
  const state = { epoch: 1, workEpoch: 1, activeWork: ref({ work_id: 'work' }), activeTask: ref(null),
    activeConversationId: ref('chat'), pendingTaskRequest: ref('request'), pendingText: ref('讨论'),
    busy: ref(false), error: ref(''), refreshConnectionError: ref(''), failureText: String,
    async getComicWorkTurn() { return { phase: 'completed', result: { conversation_response: true } } },
    async refresh() {}, async loadConversationMessages() { messages++ },
    rememberTaskRequest(_id, value) { state.pendingTaskRequest.value = value } }
  await workspaceFunction('recoverTaskRequest', '\nconst previousRunIds =', state, 'let recoveringTaskRequest=null')()
  assert.equal(messages, 1)
  assert.equal(state.pendingTaskRequest.value, '')
  assert.equal(state.error.value, '')
})

test('a failed pre-run request ends synchronization and restores the original error without a new submission', async () => {
  const core = readFileSync(new URL('../src/services/core.ts', import.meta.url), 'utf8')
  const errorCode = ts.transpileModule(core.slice(core.indexOf('export class CoreApiError'),
    core.indexOf('\nasync function apiFailure')), { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText.replace(/^export /gm, '')
  const CoreApiError = new Function(`${errorCode}; return CoreApiError`)()
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const textCode = ts.transpileModule(view.slice(view.indexOf('function failureText('),
    view.indexOf('\nasync function confirm(')), { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const failureText = new Function('CoreApiError', `${textCode}; return failureText`)(CoreApiError)
  const state = { epoch: 1, workEpoch: 1, activeWork: ref({ work_id: 'work' }), activeTask: ref(null),
    activeConversationId: ref('chat'), pendingTaskRequest: ref('request'), pendingText: ref('原输入'),
    busy: ref(false), error: ref(''), refreshConnectionError: ref(''), CoreApiError, failureText,
    async getComicWorkTurn() { return { phase: 'failed', result: null,
      error: { safe_message: '本轮来源尚未核验', error_id: 'ERR-original', trace_id: 'trace-original' } } },
    async refresh() {}, async loadConversationMessages() {},
    rememberTaskRequest(_id, value) { state.pendingTaskRequest.value = value },
    sendComicWorkTurn() { throw new Error('failed request queries cannot submit again') } }
  await workspaceFunction('recoverTaskRequest', '\nconst previousRunIds =', state, 'let recoveringTaskRequest=null')()
  assert.equal(state.pendingTaskRequest.value, '')
  assert.match(state.error.value, /本轮来源尚未核验.*ERR-original.*trace-original/)
  assert.equal(state.refreshConnectionError.value, '')
})

test('a stale empty work snapshot discovers the first saved task through reads alone', async () => {
  const work = { work_id: 'work', conversation_id: 'chat', tasks: [] }, opened = []
  const latest = { ...work, tasks: [{ task_id: 'new-task' }] }
  const state = { epoch: 1, project: ref(null), activeWork: ref(work), busy: ref(false), route: ref({}),
    async getComicWork() { return latest }, async openWorkspace(...args) { opened.push(args) } }
  const refresh = workspaceFunction('refresh', '\nasync function execute(', state)
  await refresh()
  assert.deepEqual(opened[0], [latest, undefined, true])
  state.busy.value = true
  await refresh()
  assert.equal(opened.length, 1)
})

test('conversation text shares one in-flight read and never waits for attachment downloads', async () => {
  const reply = deferredWorkspace(), image = deferredWorkspace()
  let reads = 0
  const state = { epoch: 1, activeWork: ref({ work_id: 'work' }), activeConversationId: ref('chat'),
    conversationMessages: ref([]), referenceUrls: ref({}), refreshConnectionError: ref(''), failureText: String,
    getComicWorkConversation() { reads++; return reply.promise }, openReference() { return image.promise } }
  const load = workspaceFunction('loadConversationMessages', '\nconst chatTurns =', state, 'let messageRequest=null')
  const first = load('chat'), second = load('chat')
  await flushWorkspace()
  assert.equal(reads, 1)
  reply.resolve({ messages: [{ id: 'saved', role: 'assistant', content: '导演稿已保存', artifact_id: 'image' }] })
  await Promise.all([first, second])
  assert.equal(state.conversationMessages.value[0].id, 'saved')
  image.resolve()
})

test('cancelled message reads preserve text and late reads cannot overwrite a revisited conversation', async () => {
  const requests = []
  const state = { epoch: 1, activeWork: ref({ work_id: 'work' }), activeConversationId: ref('chat'),
    conversationMessages: ref([{ id: 'existing' }]), referenceUrls: ref({}), refreshConnectionError: ref(''), failureText: String,
    getComicWorkConversation() { const request = deferredWorkspace(); requests.push(request); return request.promise } }
  const load = workspaceFunction('loadConversationMessages', '\nconst chatTurns =', state, 'let messageRequest=null')
  const cancelled = load('chat'); await flushWorkspace(); requests[0].resolve(null); await cancelled
  assert.equal(state.conversationMessages.value[0].id, 'existing')
  const old = load('chat'); await flushWorkspace()
  state.epoch++
  const current = load('chat'); await flushWorkspace()
  requests[2].resolve({ messages: [{ id: 'current', role: 'assistant', content: '新结果' }] }); await current
  requests[1].resolve({ messages: [{ id: 'stale', role: 'assistant', content: '过期结果' }] }); await old
  assert.equal(state.conversationMessages.value[0].id, 'current')
})

function refreshWorkspaceState() {
  const task = { task_id: 'task', project_id: 'project', conversation_id: 'chat' }
  const work = { work_id: 'work', tasks: [task] }
  return { epoch: 1, workEpoch: 1, project: ref({ project: { project_id: 'project' } }), activeConversationId: ref('chat'),
    activeWork: ref(work), activeTask: ref(task), workspaceWorks: ref([work]), runs: ref({}), executions: ref([]),
    assets: ref([]), works: ref([]), selectedWorkId: ref(''), selectedRun: ref(''), refreshConnectionError: ref(''),
    productionRun: ref(null), inspectorOpen: ref(false), active: ref(null), ownsWorkspaceRun: () => true,
    async getComicProject() { return null }, async getDirectorExecutions() { return [] },
    async getComicAssets() { return [] }, async getComicWork() { return work }, async loadConversationMessages() {},
    getComicConversationWorks() { throw new Error('modern work must not request legacy work projections') } }
}

test('refresh starts messages before slow metadata and reuses initial context and completed Run details', async () => {
  const state = refreshWorkspaceState(), metadata = deferredWorkspace()
  const counts = { messages: 0, projects: 0, works: 0, runs: [] }
  state.runs.value.done = { id: 'done', status: 'completed', state: { project_id: 'project' } }
  state.getComicProject = async () => { counts.projects++; return null }
  state.getComicWork = async () => { counts.works++; return state.activeWork.value }
  state.getComicAssets = () => metadata.promise
  state.loadConversationMessages = async () => { counts.messages++ }
  state.getDirectorExecutions = async () => [{ run_id: 'done', status: 'completed' }, { run_id: 'active', status: 'running' }]
  state.getRun = async id => { counts.runs.push(id); return { id, status: 'running', state: { project_id: 'project' } } }
  const refresh = workspaceFunction('refresh', '\nasync function execute(', state, 'let refreshing=null')
  const first = refresh({ context: state.project.value, workspace: state.activeWork.value })
  const duplicate = refresh()
  await flushWorkspace()
  assert.equal(counts.messages, 1)
  assert.equal(counts.projects, 0)
  assert.equal(counts.works, 0)
  metadata.resolve([])
  await Promise.all([first, duplicate])
  assert.deepEqual(counts.runs, ['active'])
  assert.equal(state.activeTask.value.task_id, 'task')
  await refresh({ context: state.project.value, workspace: state.activeWork.value })
  assert.deepEqual(counts.runs, ['active', 'active'])
})

test('refresh ownership survives out-of-order selection changes and a failed request can refresh again', async () => {
  const state = refreshWorkspaceState(), requests = []
  state.getComicAssets = () => { const request = deferredWorkspace(); requests.push(request); return request.promise }
  const refresh = workspaceFunction('refresh', '\nasync function execute(', state, 'let refreshing=null')
  const first = refresh(); await flushWorkspace()
  state.epoch++; state.workEpoch++
  const second = refresh(); await flushWorkspace()
  assert.equal(requests.length, 2)
  requests[0].resolve([{ asset_id: 'old' }]); await first
  const duplicate = refresh(); await flushWorkspace()
  assert.equal(requests.length, 2)
  requests[1].resolve([{ asset_id: 'current' }]); await Promise.all([second, duplicate])
  assert.equal(state.assets.value[0].asset_id, 'current')
  const failure = refresh(); await flushWorkspace(); requests[2].reject(new Error('offline'))
  await assert.rejects(failure, /offline/)
  const recovered = refresh(); await flushWorkspace(); requests[3].resolve([]); await recovered
  assert.equal(requests.length, 4)
})

test('opening a task renders text before context and skips story endpoints on the conversation page', async () => {
  const context = deferredWorkspace(), messages = deferredWorkspace(), order = []
  const task = { task_id: 'task', project_id: 'project', conversation_id: 'chat' }
  const work = { work_id: 'work', tasks: [task] }
  const state = { epoch: 0, workEpoch: 0, storyWorkbench: { dirty: ref(false), load() { throw new Error('story must remain lazy') } },
    activeWork: ref(null), activeTask: ref(null), activeConversationId: ref(''), drafts: ref({}), queuedInputs: ref([]),
    pendingTaskRequest: ref(''), loading: ref(true), error: ref(''), project: ref(null), selectedRun: ref(''),
    selectedStage: ref(''), section: ref('director'), chatExpanded: ref(true), workExecutions: ref([]),
    route: ref({ workspacePage: 'conversation' }), sessionStorage: { getItem() { return null } },
    resetConversationView() {}, restoreTaskRequest() {}, failureText: String, toInternalSection: page => page, toNavId: page => page,
    getComicProject() { order.push('context'); return context.promise },
    loadConversationMessages() { order.push('messages'); return messages.promise },
    async refresh(initial) { order.push('refresh'); assert.equal(initial.context.project.project_id, 'project'); assert.equal(initial.messagesLoaded, true) },
    async syncCurrentDirector() {}, async loadPage() {}, navigate() {} }
  const open = workspaceFunction('openImageTask', '\nasync function addImageTask(', state)
  const opening = open('work', 'task', false, '', work)
  await flushWorkspace()
  assert.equal(order.includes('messages'), true)
  messages.resolve(); await flushWorkspace()
  assert.equal(state.loading.value, false)
  assert.equal(order.includes('refresh'), false)
  context.resolve({ project: { project_id: 'project' } }); await opening
  assert.equal(state.error.value, '')
  assert.equal(order.filter(item => item === 'context').length, 1)
})

test('legacy conversation text renders before its work projections and Run requests finish', async () => {
  const metadata = deferredWorkspace()
  const state = { epoch: 0, workspaceWorks: ref([]), activeWork: ref(null), activeTask: ref(null), activeConversationId: ref(''),
    loading: ref(true), error: ref(''), chatExpanded: ref(true), inspectorOpen: ref(false), conversationMessages: ref([]),
    storyWorkbench: { dirty: ref(false) }, localStorage: { setItem() {} }, route: ref({}), resetConversationView() {},
    navigate() {}, failureText: String, works: ref([]), selectedWorkId: ref(''), runs: ref({}),
    productionRun: ref(null), currentDirector: ref(null), composer: ref(null), nextTick,
    async getConversation() { return { messages: [{ id: 'text', role: 'assistant', content: '已保存' }] } },
    getComicConversationWorks() { return metadata.promise }, async getRun() { return null }, conversationWorkspaceRun }
  const open = workspaceFunction('openConversation', '\nasync function finishConversationRename(', state)
  const pending = open('chat'); await flushWorkspace()
  assert.equal(state.loading.value, false)
  assert.equal(state.conversationMessages.value[0].id, 'text')
  metadata.resolve([]); await pending
  assert.equal(state.error.value, '')
})

test('story and export share an in-flight load while conversation entry loads no story data', async () => {
  const story = deferredWorkspace(); let requests = 0
  const state = { epoch: 1, section: ref('director'), project: ref(null), activeConversationId: ref('chat'),
    storyOwner: ref('work:work'), storyWorkbench: { load() { requests++; return story.promise } } }
  const load = workspaceFunction('loadPage', '\nasync function loadShots(', state, 'let pageRequest=0, storyRequest=null')
  await load(); assert.equal(requests, 0)
  state.section.value = 'story'; const first = load()
  state.section.value = 'export'; const second = load()
  assert.equal(requests, 1)
  story.resolve(); await Promise.all([first, second])
})

test('a first creative reply selects the new task from refreshed work data, never its pre-submit snapshot', async () => {
  const current = { work_id: 'work', version: 1, conversation_id: 'chat', tasks: [] }
  const latest = { ...current, version: 2, tasks: [{ task_id: 'new-task' }] }
  const opened = []
  const state = { epoch: 1, workEpoch: 1, busy: ref(false), conversationBusy: ref(false), hasRunning: ref(false),
    pendingTaskRequest: ref(''), pendingRequestId: ref(''), activeWork: ref(current), activeTask: ref(null),
    pendingText: ref(''), submittingDirector: ref(false), error: ref(''), executions: ref([]), previousRunIds: ref([]),
    workspaceWorks: ref([current]), async getComicWork() { return current }, rememberTaskRequest() {},
    async sendComicWorkTurn() { return { run_id: 'new-run', task_id: 'new-task' } },
    async loadWorks() { state.workspaceWorks.value = [latest] },
    async openImageTask(...args) { opened.push(args) }, failureText: String }
  const execute = workspaceFunction('executeTaskTurn', '\nfunction sendInput(', state)
  await execute('首次生成导演方案', 'professional')
  assert.equal(state.error.value, '')
  assert.equal(opened[0][1], 'new-task')
  assert.equal(opened[0][4].version, 2)
  assert.equal(opened[0][4].tasks[0].task_id, 'new-task')
})

test('initial known work or conversation opens while the recent-history request is still pending', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = view.slice(view.indexOf('onMounted(async () => {'), view.indexOf('\nonBeforeUnmount('))
  const code = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText
  for (const modern of [false, true]) {
    const works = deferredWorkspace(), history = deferredWorkspace(), opened = []
    const work = { work_id: 'work', tasks: [] }
    const state = { loading: ref(true), error: ref(''), refreshConnectionError: ref(''),
      workspaceWorks: ref(modern ? [work] : []), conversations: ref([]),
      route: ref(modern ? { workId: 'work', workspacePage: 'conversation' } : { conversationId: 'chat', workspacePage: 'conversation' }),
      props: {}, localStorage: { getItem() { return null } }, scheduleRefresh() {}, failureText: String,
      loadWorks() { return works.promise }, loadConversations(worksReady) { assert.equal(worksReady, works.promise); return history.promise },
      async openWorkspace(work) { opened.push(work.work_id); state.loading.value = false },
      async openConversation(id) { opened.push(id); state.loading.value = false } }
    const mounted = new Function('state', `const {${Object.keys(state).join(',')}}=state; const disposed=false; let mounted; function onMounted(callback){mounted=callback}; ${code}; return mounted`)(state)
    const pending = mounted(); await flushWorkspace()
    assert.deepEqual(opened, [])
    works.resolve(); await flushWorkspace()
    assert.deepEqual(opened, [modern ? 'work' : 'chat'])
    assert.equal(state.loading.value, false)
    history.resolve(); await pending
    assert.equal(state.error.value, '')
  }
})

test('returning through the bare workspace entry never creates another work', async () => {
  const view = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const code = ts.transpileModule(view.slice(view.indexOf('onMounted(async () => {'),
    view.indexOf('\nonBeforeUnmount(')), { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const existing = { work_id: 'existing-work', tasks: [] }
  const opened = [], created = []
  for (const selected of [{}, { workId: 'existing-work', workspacePage: 'images' },
    { workId: 'missing-work' }, { workId: 'existing-work' }]) {
    const state = { loading: ref(true), error: ref(''), refreshConnectionError: ref(''),
      workspaceWorks: ref([existing]), conversations: ref([]), route: ref(selected), props: {},
      localStorage: { getItem() { return null } }, scheduleRefresh() {}, failureText: String,
      async loadWorks() {}, async loadConversations() {},
      async openWorkspace(...args) { opened.push(args) },
      async createNewWorkspace() { created.push('unexpected creation') } }
    const mounted = new Function('state', `const {${Object.keys(state).join(',')}}=state; const disposed=false; let mounted; function onMounted(callback){mounted=callback}; ${code}; return mounted`)(state)
    await mounted()
    assert.equal(created.length, 0)
    if (selected.workId === 'missing-work') assert.match(state.error.value, /无法读取指定作品/)
    else assert.deepEqual(opened.at(-1)[0], existing)
  }
  assert.equal(opened.length, 3)
  assert.equal(opened.every(args => args[2] === true), true)
  const explicit = []
  const state = { conversationBusy: ref(false), error: ref(''), failureText: String,
    async createComicWork() { explicit.push('create'); return { work_id: 'new-work' } },
    async loadWorks() {}, async openWorkspace(work) { explicit.push(work.work_id) } }
  await workspaceFunction('createNewWorkspace', '\nasync function openWorkspace(', state)()
  assert.deepEqual(explicit, ['create', 'new-work'])
})

test('parallel history fetch waits for the work list before hiding migrated task conversations', async () => {
  const works = deferredWorkspace()
  let requested = false
  const state = { workspaceWorks: ref([]), conversations: ref([]),
    async getConversations() { requested = true; return [{ id: 'task-chat', interaction_mode: 'guided' }, { id: 'legacy', interaction_mode: 'guided' }] } }
  const load = workspaceFunction('loadConversations', '\nasync function loadWorks(', state)
  const pending = load(works.promise)
  assert.equal(requested, true)
  assert.deepEqual(state.conversations.value, [])
  state.workspaceWorks.value = [{ story_conversation_id: 'story', tasks: [{ conversation_id: 'task-chat' }] }]
  works.resolve(); await pending
  assert.deepEqual(state.conversations.value.map(item => item.id), ['legacy'])
})

function appCoreHarness(initialRoute) {
  const appSource = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
  const script = parse(appSource).descriptor.scriptSetup.content.replace(/^import .*$/gm, '')
  const code = ts.transpileModule(script, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText
  const intervals = new Map(), requests = [], stops = []
  let mounted, unmounted, timerId = 0, disposed = 0
  const currentRoute = ref(initialRoute)
  const environment = { ref, computed, route: currentRoute, Boxes: {}, Settings: {},
    watch(...args) { const stop = watch(...args); stops.push(stop); return stop },
    onMounted(callback) { mounted = callback }, onBeforeUnmount(callback) { unmounted = callback },
    window: { matchMedia() { return { matches: false } }, addEventListener() {}, removeEventListener() {},
      setInterval(callback, delay) { assert.equal(delay, 4000); intervals.set(++timerId, callback); return timerId },
      clearInterval(id) { intervals.delete(id) } },
    getRuns() { const request = { runs: deferredWorkspace(), approvals: deferredWorkspace() }; requests.push(request); return request.runs.promise },
    getApprovals() { return requests[requests.length - 1].approvals.promise }, disposeAll() { disposed++ } }
  const app = new Function('environment', `const {${Object.keys(environment).join(',')}}=environment; ${code}; return {refreshCore, runs, approvals}`)(environment)
  return { ...app, route: currentRoute, intervals, requests,
    mount() { mounted() }, close() { unmounted(); stops.forEach(stop => stop()); assert.equal(disposed, 1) } }
}

test('App pauses unrelated global Core lists throughout Comic workspace and resumes immediately on exit', async () => {
  const app = appCoreHarness({ name: 'workspace', domain: 'comic' })
  try {
    app.mount(); await flushWorkspace(); await app.refreshCore()
    assert.equal(app.requests.length, 0)
    assert.equal(app.intervals.size, 0)
    app.route.value = { name: 'workspace_run', domain: 'comic', runId: 'director' }
    await flushWorkspace()
    assert.equal(app.requests.length, 0)
    app.route.value = { name: 'home' }; await flushWorkspace()
    assert.equal(app.requests.length, 1)
    assert.equal(app.intervals.size, 1)
    const callback = [...app.intervals.values()][0]
    callback(); callback(); const shared = app.refreshCore(); await flushWorkspace()
    assert.equal(app.requests.length, 1)
    app.requests[0].runs.resolve([{ id: 'current-run' }]); app.requests[0].approvals.resolve([{ id: 'approval' }])
    await shared
    assert.equal(app.runs.value[0].id, 'current-run')
    assert.equal(app.approvals.value[0].id, 'approval')
    app.route.value = { name: 'tasks' }; await flushWorkspace()
    assert.equal(app.requests.length, 1)
    assert.equal(app.intervals.size, 1)
    app.route.value = { name: 'workspace', domain: 'comic' }; await flushWorkspace()
    assert.equal(app.intervals.size, 0)
    await app.refreshCore(); assert.equal(app.requests.length, 1)
  } finally { app.close() }
})

test('App rejects obsolete Core responses across Comic navigation without clearing a newer in-flight request', async () => {
  const app = appCoreHarness({ name: 'home' })
  try {
    app.mount(); await flushWorkspace()
    app.route.value = { name: 'workspace', domain: 'comic' }
    app.route.value = { name: 'assets' }; await flushWorkspace()
    assert.equal(app.requests.length, 2)
    app.requests[0].runs.resolve([{ id: 'obsolete' }]); app.requests[0].approvals.resolve([{ id: 'obsolete' }])
    await flushWorkspace()
    assert.deepEqual(app.runs.value, [])
    assert.deepEqual(app.approvals.value, [])
    const current = app.refreshCore(); await flushWorkspace()
    assert.equal(app.requests.length, 2)
    app.requests[1].runs.resolve([{ id: 'fresh' }]); app.requests[1].approvals.resolve([{ id: 'fresh-approval' }])
    await current
    assert.equal(app.runs.value[0].id, 'fresh')
    assert.equal(app.approvals.value[0].id, 'fresh-approval')
  } finally { app.close() }
  await app.refreshCore()
  assert.equal(app.requests.length, 2)
  assert.equal(app.intervals.size, 0)
})
