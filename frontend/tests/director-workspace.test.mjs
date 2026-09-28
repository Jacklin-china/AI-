import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/domains/comic/directorPresentation.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const { canConfirmDirector, chronologicalDirectorExecutions, directorIsStale, directorSummary, draftConfirmationKey, stageDraftKey, publicDirectorSections } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const spec = {
  schema_version: 2, spec_id: 'director', version: 2, creative_brief_version: 3,
  creative_decision: { intent_summary: '异兽观察文明', emotional_target: '孤独', private_thought: 'must not show' },
  director_plan: { visual_strategy: '留出环境空间', composition_strategy: '人物与城市遥相呼应' },
  cinematography: { camera_angle: '平视', light_direction: '侧光' }, critic_result: { verdict: 'pass' },
  asset_versions: { 'asset:character': 1 },
}
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
test('confirmation belongs to one immutable project/spec/version and never to a mode or Run success flag', () => {
  const key = draftConfirmationKey('project', spec)
  assert.equal(key, draftConfirmationKey('project', structuredClone(spec)))
  assert.notEqual(key, draftConfirmationKey('other', spec))
  assert.notEqual(key, draftConfirmationKey('project', { ...spec, version: 3 }))
  assert.notEqual(key, draftConfirmationKey('project', { ...spec, cinematography: { camera_angle: '俯视' } }))
  assert.equal(draftConfirmationKey('project', null), '')
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
  assert.match(view, /!\['failed', 'waiting'\]\.includes\(active\.value\?\.status/)
  assert.match(view, /if \(failedRun\) \{ selectedRun\.value = failedRun\.run_id/)
})
