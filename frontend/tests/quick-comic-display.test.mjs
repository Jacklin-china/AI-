import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'

async function load(path) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
}
const { quickDirectorMessage } = await load('../src/domains/comic/directorPresentation.ts')
const { comicProductionProgress, isComicFastImage } = await load('../src/domains/comic/productionProgress.ts')

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

test('automatic homepage image task shows particles from real analysis, not director reports', () => {
  const fast = { ...run, current_node: 'director', state: { execution_mode: 'fast', quick_creation: { auto_create_image: true } } }
  assert.equal(isComicFastImage(fast), true)
  assert.equal(isComicFastImage({ ...fast, state: { ...fast.state, execution_mode: 'professional' } }), false)
  const progress = comicProductionProgress(fast, events.slice(0, 1), false)
  assert.equal(progress.label, '正在分析需求')
  assert.equal(progress.visible, true)
  assert.equal(progress.percent, 10)
  assert.equal(comicProductionProgress({ ...fast, current_node: 'prompt' }, events, false).label, '正在优化提示词')
  assert.equal(comicProductionProgress({ ...fast, status: 'waiting', current_node: 'director_gate' }, events, false).visible, false)
  assert.equal(comicProductionProgress({ ...fast, status: 'waiting', current_node: 'generate' }, events, false).visible, true)
})

// Render the real message-list template; child stubs preserve their public props.
// This is a rendering contract, not a browser layout or provider acceptance test.
const source = readFileSync(new URL('../src/components/chat/ChatMessageList.vue', import.meta.url), 'utf8')
const script = compileScript(parse(source).descriptor, { id: 'fast-image-messages', inlineTemplate: true }).content
const progressCode = ts.transpileModule(readFileSync(new URL('../src/domains/comic/productionProgress.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const progressUrl = `data:text/javascript;base64,${Buffer.from(progressCode).toString('base64')}`
const vueUrl = import.meta.resolve('vue')
const compiled = ts.transpileModule(script, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  .replace(/from (["'])vue\1/g, `from ${JSON.stringify(vueUrl)}`)
  .replace(/from (["'])lucide-vue-next\1/g, `from ${JSON.stringify(import.meta.resolve('lucide-vue-next'))}`)
  .replace(/from (["'])\.\.\/\.\.\/domains\/comic\/productionProgress\1/g, `from ${JSON.stringify(progressUrl)}`)
  .replace(/import \{ presenterFor \} from [^;]+;/, 'const presenterFor = () => ({ nodeLabel: id => id });')
  .replace(/import (\w+) from ["'][^"']+\.vue["'];/g, (_all, name) => {
    const stub = `import { h } from ${JSON.stringify(vueUrl)};
      export default { props: ['content','media','label','percent'], setup: p => () => h('div', { 'data-component': '${name}' }, p.content ?? p.media?.url ?? (p.label ? p.label + ' 阶段进度 ' + p.percent + '%' : '')) };`
    return `import ${name} from ${JSON.stringify(`data:text/javascript;base64,${Buffer.from(stub).toString('base64')}`)};`
  })
const { default: MessageList } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const messages = [
  { id: 'user-1', role: 'user', content: '穷奇悬崖场景' },
  { id: 'plan-1', role: 'assistant', content: '{"creative_decision":"private-report-marker"}', event_id: 'quick-director:run-1' },
]
function renderMessages(overrides) {
  return renderToString(createSSRApp(MessageList, { messages, streaming: '', run: null, error: '', homeMode: true, ...overrides }))
}
test('homepage actual template hides director reports and renders the stage loader', async () => {
  const task = { ...run, id: 'run-1', current_node: 'director', state: { execution_mode: 'fast', quick_creation: { auto_create_image: true } } }
  const html = await renderMessages({ inlineRuns: { 'user-1': { run: task, activities: events.slice(0, 1), approval: null, artifact: null, imageUrl: '', videoUrl: '' } } })
  assert.match(html, /正在分析需求 阶段进度 10%/)
  assert.doesNotMatch(html, /creative_decision|private-report-marker|chat-run-feed/)
})
test('completed image replaces the loader once; persisted artifact still renders without inline state', async () => {
  const url = '/api/artifacts/real-image/content'
  const result = { id: 'result-1', role: 'assistant', content: '已生成当前镜头图片。', event_id: 'quick-image:run-1', run_id: 'run-1', artifact_id: 'real-image' }
  const persisted = { messages: [...messages, result], messageMedia: { 'result-1': { type: 'image', url, filename: 'image.png' } } }
  const html = await renderMessages({ ...persisted, inlineRuns: { 'user-1': { run: { ...run, id: 'run-1', status: 'completed', state: { execution_mode: 'fast', quick_creation: { auto_create_image: true } } }, activities: events, approval: null, artifact: { type: 'image' }, imageUrl: url, videoUrl: '' } } })
  assert.equal(html.split(url).length - 1, 1)
  assert.doesNotMatch(html, /ImageGenerationPlaceholder|private-report-marker/)
  assert.match(await renderMessages(persisted), /\/api\/artifacts\/real-image\/content/)
})
