import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { reactive } from 'vue'

const source = readFileSync(new URL('../src/domains/comic/directorPresentation.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const { discardDirectorNodeDraft, directorPageFields, directorPageFieldLabel, fastDirectorNodeLabels, directorDraftFields, editableDirectorDraft, canConfirmDirector, canDispatchDirectorInput, chronologicalDirectorExecutions, directorIsStale, directorSummary, stageDraftKey, publicDirectorSections, workspaceProjectTitle } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const spec = {
  schema_version: 2, spec_id: 'director', version: 2, creative_brief_version: 3,
  creative_decision: { intent_summary: '异兽观察文明', emotional_target: '孤独', private_thought: 'must not show' },
  director_plan: { visual_strategy: '留出环境空间', composition_strategy: '人物与城市遥相呼应' },
  cinematography: { camera_angle: '平视', light_direction: '侧光' }, critic_result: { verdict: 'pass' },
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
  for (const status of ['pending', 'running', 'waiting', 'failed']) assert.equal(canConfirmDirector(status, spec, false, false), false)
  assert.equal(canConfirmDirector('completed', spec, true, false), false)
  assert.equal(canConfirmDirector('completed', spec, false, true), false)
  assert.equal(canConfirmDirector('completed', { ...spec, critic_result: { verdict: 'needs_revision' } }, false, false), false)
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
  assert.match(view, /Splitpanes horizontal/)
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
  assert.match(view, /const id = String\(legacy \? legacy\.state\.project_id/)
  assert.match(view, /board\?\.director_spec_version !== spec\.value\?\.version/)
  assert.match(view, /restoreChoice === Number\(version\.version\)/)
  assert.match(view, /latestVersion && !tasks.some/)
  assert.match(view, /creative_operation: 'new'/)
  assert.match(view, /spec\.value\?\.user_confirmed === true/)
  assert.match(view, /confirmDirectorVersion/)
  assert.doesNotMatch(view, /kantoku-director-confirmation:/)
  assert.match(view, /if \(failedRun\) \{ selectedRun\.value = failedRun\.run_id/)
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
