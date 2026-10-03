import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

async function load(path) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
}
const { quickDirectorMessage } = await load('../src/domains/comic/directorPresentation.ts')
const { comicProductionProgress } = await load('../src/domains/comic/productionProgress.ts')

test('public structured director output becomes readable Markdown, not raw JSON', () => {
  const text = quickDirectorMessage(JSON.stringify({ status: 'completed', director_spec: {
    creative_decision: { intent_summary: '牛爷爷打电话', hard_constraints: ['保留主体', 'JOJO 风格'], private_reasoning: 'secret-thought' },
    director_plan: { visual_strategy: JSON.stringify({ composition_strategy: '半身构图', color_strategy: '明快色彩', provider: 'hidden-provider' }) },
    cinematography: { public_decision: JSON.stringify({ shot_size: '中景', camera_angle: '平视' }), structured_plan: { lighting: '柔和光线' } },
    project_id: 'hidden-project',
  } }))
  for (const fragment of ['### 创意理解', '### 导演方案', '### 摄影 / 画面建议', '半身构图', '柔和光线', '- 保留主体', '**确认方案并生成**']) assert.ok(text.includes(fragment), fragment)
  for (const fragment of ['intent_summary', 'public_decision', 'shot_size', 'hidden-provider', 'hidden-project', 'secret-thought', '{', '}']) assert.ok(!text.includes(fragment), fragment)
})

test('legacy persisted director JSON strings and malformed dicts never leak field dumps', () => {
  const legacy = quickDirectorMessage('创意理解：牛爷爷打电话\n\n导演方案：{"visual_strategy":"简洁背景"}\n\n摄影方案：{"shot_size":"中景"}')
  assert.ok(legacy.includes('### 导演方案\n\n**视觉策略**：简洁背景'))
  assert.ok(!legacy.includes('shot_size'))
  assert.ok(!quickDirectorMessage("{'status': 'failed'}").includes('status'))
  assert.ok(!quickDirectorMessage('null').includes('null'))
  assert.ok(quickDirectorMessage('{}').includes('暂未进入生图'))
  assert.ok(!quickDirectorMessage("```json\n{'status': 'failed'}\n```").includes('status'))
})

test('a real failed review shows public correction advice without exposing private review data', () => {
  const text = quickDirectorMessage(JSON.stringify({ status: 'waiting', director_spec: {
    creative_decision: { intent_summary: '本次人物' }, critic_result: {
      public_summary: '人物和场景关系需要调整', findings: [{ field_path: 'director_plan.visual_focus', suggested_action: '增加环境占比' }],
    },
  } }))
  assert.ok(text.includes('暂未进入生图'))
  assert.ok(text.includes('增加环境占比'))
  assert.ok(!text.includes('field_path'))
})

const run = { domain: 'comic', status: 'running', current_node: 'generate', state: { quick_creation: {} } }
const events = ['cost_approval', 'director', 'director_gate', 'storyboard', 'prompt', 'prepare'].map((node_id, sequence) => ({ node_id, sequence, event_type: 'node_completed' }))
test('phase progress depends only on real completed events and survives reload', () => {
  const initial = comicProductionProgress(run, events, false)
  assert.equal(initial.percent, 60)
  assert.equal(initial.visible, true)
  assert.equal(comicProductionProgress({ ...run, status: 'waiting' }, events, false).percent, initial.percent)
  assert.deepEqual(comicProductionProgress(run, JSON.parse(JSON.stringify(events)), false), initial)
  assert.equal(comicProductionProgress(run, [...events, { node_id: 'generate', sequence: 9, event_type: 'node_started' }], false).percent, 60)
})
test('100% requires completed Run AND a real image; errors never become success', () => {
  assert.equal(comicProductionProgress({ ...run, status: 'completed' }, events, true).percent, 100)
  assert.notEqual(comicProductionProgress({ ...run, status: 'completed' }, events, false).percent, 100)
  assert.notEqual(comicProductionProgress({ ...run, status: 'failed' }, events, true).percent, 100)
  assert.equal(comicProductionProgress({ ...run, status: 'failed' }, events, false).visible, false)
  assert.equal(comicProductionProgress({ ...run, current_node: 'director_gate' }, events, false).visible, false)
  assert.equal(comicProductionProgress(run, events, true).visible, false)
})
test('retrying a stage does not retain its previous completion percentage', () => {
  const retry = [...events, { node_id: 'prepare', sequence: 10, event_type: 'node_retrying' }]
  assert.equal(comicProductionProgress(run, retry.reverse(), false).percent, 50)
})
