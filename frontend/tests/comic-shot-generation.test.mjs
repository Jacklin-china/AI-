import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'

const compile = code => ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const moduleUrl = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const { shotGeneration, externalShotPrompt, finalImagePrompt, promptVersionDiff } = await import(moduleUrl(compile(readFileSync(new URL('../src/domains/comic/shotGeneration.ts', import.meta.url), 'utf8'))))
const component = readFileSync(new URL('../src/components/ComicShotGeneration.vue', import.meta.url), 'utf8')
const compiled = compile(compileScript(parse(component).descriptor, { id: 'shot-generation', inlineTemplate: true }).content)
  .replace(/from (["'])vue\1/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`)
  .replace(/import ImageGenerationPlaceholder from [^;]+;/, `const ImageGenerationPlaceholder = { props: ['label'], template: '<div data-image-loader>{{label}}</div>' };`)
  .replace(/import ChatImageAttachment from [^;]+;/, `const ChatImageAttachment = { props: ['media'], template: '<img :src="media.url" />' };`)
const { default: ShotView } = await import(moduleUrl(compiled))
const run = (id, shotId, status = 'running', projectId = 'project') => ({
  id, workflow: 'comic.production.v1', status, started_at: `2026-10-05T00:00:0${id.slice(-1)}Z`,
  state: { request_id: `attempt-${id}`, quick_creation: { project_id: projectId, shot_id: shotId } },
  image_execution: { can_regenerate: status === 'failed', can_resume: status === 'waiting', model: 'configured-model', actual_fen: null },
})

test('only the generating Shot renders a loader; every navigation page remains free of image loaders', async () => {
  const runs = [run('run1', 'shot1'), run('run2', 'shot2', 'completed', 'other-project')]
  const image = await renderToString(createSSRApp({ render: () => h('section', ['shot1', 'shot2'].map(shotId => {
    const current = shotGeneration(runs, 'project', shotId)
    return h('article', { 'data-shot-id': shotId }, [h(ShotView, { run: current, disabled: false,
      progress: current?.status === 'running' ? { visible: true, label: '正在生成图片', percent: 60 } : null })])
  })) }))
  assert.equal((image.match(/data-image-loader/g) ?? []).length, 1)
  assert.match(image, /data-shot-id="shot1"[^]*data-generation-id="attempt-run1"/)
  const otherShot = image.slice(image.indexOf('data-shot-id="shot2"'))
  assert.doesNotMatch(otherShot, /data-image-loader|attempt-run1/)
  const workspace = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const { descriptor } = parse(workspace)
  assert.doesNotMatch(descriptor.template.content, /<ImageGenerationPlaceholder/)
  const shotSection = descriptor.template.content.slice(descriptor.template.content.indexOf('<template v-if="section === \'storyboard\'">'), descriptor.template.content.indexOf('<template v-else><label v-if="shots.length">'))
  assert.match(shotSection, /<ComicShotGeneration/)
  assert.equal((descriptor.template.content.match(/<ComicShotGeneration/g) ?? []).length, 1)
})

test('failed shot remains retryable; new attempt replaces only that shots displayed state', async () => {
  const failed = run('run1', 'shot1', 'failed')
  const errorHtml = await renderToString(createSSRApp(ShotView, { run: failed, disabled: false }))
  assert.match(errorHtml, /data-status="failed"/)
  assert.match(errorHtml, /重新生成/)
  assert.match(errorHtml, /修改 Prompt/)
  assert.match(errorHtml, /更换模型/)
  assert.doesNotMatch(errorHtml, /data-image-loader/)
  const retry = run('run2', 'shot1')
  assert.equal(shotGeneration([failed, retry], 'project', 'shot1').id, 'run2')
  assert.equal(shotGeneration([failed, retry], 'project', 'shot2'), null)
  assert.equal(shotGeneration([failed, retry], 'other', 'shot1'), null)
})

test('external Shot preparation renders no image progress, provider failure or payment controls', async () => {
  const prompt = { artifact_id: 'prompt-artifact', version: 1, model_target: 'external', shot_version: 2, director_spec_version: 3 }
  const legacyFailure = run('run1', 'shot1', 'failed')
  const preparing = await renderToString(createSSRApp(ShotView, { run: legacyFailure, disabled: false,
    imageMode: 'external', preparing: true, progress: { visible: true, label: '正在生成图片', percent: 60 } }))
  assert.match(preparing, /正在生成完整 Image Prompt/)
  assert.doesNotMatch(preparing, /data-image-loader|正在生成图片|重新生成|图片成本|当前镜头生成失败/)
  const ready = await renderToString(createSSRApp(ShotView, { run: legacyFailure, disabled: false,
    imageMode: 'external', externalPrompt: prompt }))
  assert.match(ready, /data-media-status="awaiting_external_image"/)
  assert.match(ready, /Prompt 已生成/)
  assert.match(ready, /等待外部图片/)
  for (const label of ['查看完整 Prompt', '复制生图 Prompt', '上传图片']) assert.ok(ready.includes(label))
  assert.doesNotMatch(ready, /data-image-loader|当前镜头生成失败|确认费用|重新生成/)
  assert.equal(externalShotPrompt([prompt], 2, 3), prompt)
  assert.equal(externalShotPrompt([prompt], 1, 3), null)
  assert.equal(externalShotPrompt([prompt], 2, 4), null)
})

test('actual Shot preparation invokes only external Prompt compile and retains the selected Shot loader', async () => {
  const workspace = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const code = compile(workspace.slice(workspace.indexOf('async function compilePrompt('), workspace.indexOf('async function recoverImage(')))
  const ref = value => ({ value })
  const calls = []
  let release
  const state = {
    project: ref({ project: { project_id: 'project', current_version: 3 } }), spec: ref({ version: 1 }),
    manualDirectorApproval: ref(true), confirmed: ref(true), compiling: ref(false), busy: ref(false), hasRunning: ref(false),
    shots: ref([{ shot_id: 'shot1', version: 2 }, { shot_id: 'shot2', version: 1 }]), selectedShot: ref('shot1'),
    boards: ref([{ storyboard_id: 'board', director_spec_version: 1 }]), selectedBoard: ref('board'),
    preparingShot: ref(''), shotPrompts: ref({}), error: ref(''), activeConversationId: ref('conversation'),
    shotErrors: ref({}), promptPreview: ref(null),
    async compileComicPrompt(...args) { calls.push(args); await new Promise(resolve => { release = resolve }); return { artifact_id: 'prompt' } },
    async refresh() {}, async loadPrompts() {}, failureText: String,
  }
  const prepare = new Function('state', `const {${Object.keys(state).join(',')}}=state; const conversationEpoch=1; ${code}; return compilePrompt`)(state)
  const pending = prepare()
  assert.deepEqual(calls, [['shot1', 3, 2, 'conversation', true]])
  state.selectedShot.value = 'shot2'
  assert.equal(state.preparingShot.value, 'shot1')
  release(); await pending
  assert.equal(state.preparingShot.value, '')
  assert.equal(state.shotPrompts.value.shot1[0].artifact_id, 'prompt')
  state.confirmed.value = false
  await prepare()
  assert.equal(calls.length, 1)
  assert.doesNotMatch(workspace, /createRun\(|@click="generateImage\(\)"/)
})

test('copy and export use full Image Prompt including negative constraints; version comparison retains edits', () => {
  const record = { positive_prompt: '人物：黑发少女\n世界：夜晚山巅', negative_prompt: '现代建筑，文字水印' }
  const full = finalImagePrompt(record)
  assert.ok(full.includes('黑发少女') && full.includes('文字水印'))
  assert.equal(finalImagePrompt({ ...record, final_prompt: full }), full)
  const changes = promptVersionDiff('人物：黑发\n镜头：远景', '人物：黑发\n镜头：中景')
  assert.equal(changes.length, 1)
  assert.match(JSON.stringify(changes), /远景/)
  assert.match(JSON.stringify(changes), /中景/)
})

test('external upload displays only the attached shot image and leaves old provider failures hidden', async () => {
  const html = await renderToString(createSSRApp(ShotView, { imageMode: 'external', disabled: false,
    externalPrompt: { version: 2 }, externalImageUrl: 'blob:uploaded-shot', run: run('run1', 'shot1', 'failed') }))
  assert.match(html, /image_uploaded/)
  assert.match(html, /blob:uploaded-shot/)
  assert.doesNotMatch(html, /data-image-loader|重新生成|图片成本|确认费用/)
})

test('actual workspace copy handler submits the complete saved text and reports clipboard failure', async () => {
  const workspace = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const source = compile(workspace.slice(workspace.indexOf('async function copyImagePrompt('), workspace.indexOf('function exportImagePrompt(')))
  const copiedPrompt = { value: '' }, error = { value: '' }
  const writes = []
  const navigator = { clipboard: { async writeText(value) { writes.push(value) } } }
  const copy = new Function('navigator', 'copiedPrompt', 'error', 'finalImagePrompt', `const failureText = String; ${source}; return copyImagePrompt`)(navigator, copiedPrompt, error, finalImagePrompt)
  const prompt = { artifact_id: 'revision2', positive_prompt: '人物：黑发少女\n世界：夜晚山巅', negative_prompt: '文字水印' }
  await copy(prompt)
  assert.equal(writes[0], finalImagePrompt(prompt))
  assert.ok(writes[0].includes('Negative Constraint') && writes[0].includes('黑发少女'))
  assert.equal(copiedPrompt.value, 'revision2')
  navigator.clipboard.writeText = async () => { throw new Error('permission denied') }
  await copy(prompt)
  assert.match(error.value, /无法复制 Prompt.*permission denied/)
})
