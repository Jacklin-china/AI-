import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { createHash } from 'node:crypto'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import { computed, createSSRApp, h, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'

const compile = code => ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const moduleUrl = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const { shotGeneration, externalShotPrompt, finalImagePrompt, promptVersionDiff, imagePromptReferences, readPromptReference, copyPromptDelivery } = await import(moduleUrl(compile(readFileSync(new URL('../src/domains/comic/shotGeneration.ts', import.meta.url), 'utf8'))))
const component = readFileSync(new URL('../src/components/ComicShotGeneration.vue', import.meta.url), 'utf8')
const importerCode = compile(compileScript(parse(readFileSync(new URL('../src/components/comic/ComicImageImport.vue', import.meta.url), 'utf8')).descriptor,
  { id: 'image-import', inlineTemplate: true }).content)
  .replace(/from (["'])vue\1/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`)
  .replace(/from (["'])lucide-vue-next\1/g, `from ${JSON.stringify(import.meta.resolve('lucide-vue-next'))}`)
const compiled = compile(compileScript(parse(component).descriptor, { id: 'shot-generation', inlineTemplate: true }).content)
  .replace(/from (["'])vue\1/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`)
  .replace(/import ImageGenerationPlaceholder from [^;]+;/, `const ImageGenerationPlaceholder = { props: ['label'], template: '<div data-image-loader>{{label}}</div>' };`)
  .replace(/import ChatImageAttachment from [^;]+;/, `const ChatImageAttachment = { props: ['media'], template: '<img :src="media.url" />' };`)
  .replace(/from (["'])\.\/comic\/ComicImageImport.vue\1/, `from ${JSON.stringify(moduleUrl(importerCode))}`)
const { default: ShotView } = await import(moduleUrl(compiled))
const run = (id, shotId, status = 'running', projectId = 'project') => ({
  id, workflow: 'comic.production.v1', status, started_at: `2026-10-05T00:00:0${id.slice(-1)}Z`,
  state: { request_id: `attempt-${id}`, quick_creation: { project_id: projectId, shot_id: shotId } },
  image_execution: { can_regenerate: status === 'failed', can_resume: status === 'waiting', model: 'configured-model', actual_fen: null },
})
const source = { projectId: 'project', shotId: 'shot1', storyboardId: 'board', storyboardVersion: 1, briefVersion: 2 }
const completePrompt = overrides => ({ artifact_id: 'prompt-artifact', version: 1, model_target: 'external',
  project_id: 'project', shot_id: 'shot1', storyboard_id: 'board', storyboard_version: 1, creative_brief_version: 2,
  shot_version: 2, director_spec_version: 3, context_version: 'image-bibles-1', character_context: '身份：人物',
  world_context: '地点：山巅', style_context: '艺术风格：动画', shot_context: '动作：静立',
  positive_prompt: '人物静立山巅', negative_prompt: '文字水印', ...overrides })

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
  const prompt = completePrompt()
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
  assert.equal(externalShotPrompt([prompt], 2, 3, source), prompt)
  assert.equal(externalShotPrompt([prompt], 1, 3, source), null)
  assert.equal(externalShotPrompt([prompt], 2, 4, source), null)
})

test('ready requires the current source and a real complete prompt; a valid scene may omit character text', () => {
  for (const override of [{ project_id: 'other' }, { shot_id: 'other' }, { storyboard_id: 'other' },
    { storyboard_version: 9 }, { creative_brief_version: 1 }, { context_version: null }, { shot_context: undefined },
    { positive_prompt: ' ' }, { negative_prompt: '' }, { artifact_id: '' }]) {
    assert.equal(externalShotPrompt([completePrompt(override)], 2, 3, source), null, JSON.stringify(override))
  }
  const emptyCharacter = completePrompt({ character_context: '' })
  assert.equal(externalShotPrompt([emptyCharacter], 2, 3, source), emptyCharacter)
  const current = completePrompt()
  assert.equal(externalShotPrompt([completePrompt({ director_spec_version: 2 }), current], 2, 3, source), current)
})

test('the workflow does not report a stale project prompt ready for the current shot', () => {
  const workspace = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const selector = workspace.slice(workspace.indexOf('function externalPromptForShot('), workspace.indexOf('const comparison = computed'))
  const steps = workspace.slice(workspace.indexOf('const productionSteps = computed'), workspace.indexOf('const activeRun = computed'))
  const state = { computed, externalShotPrompt, spec: ref({ version: 3 }), workExecutions: ref([]),
    project: ref({ project: { project_id: 'project' }, creative_brief: { version: 2 } }),
    shots: ref([{ shot_id: 'shot1', storyboard_id: 'board', version: 2 }]), selectedShot: ref('shot1'),
    boards: ref([{ storyboard_id: 'board', version: 1, director_spec_version: 3 }]),
    shotPrompts: ref({ shot1: [completePrompt({ director_spec_version: 2 })] }),
    projectPrompts: ref([completePrompt({ director_spec_version: 2 })]), imageWorkbenchState: ref(null),
    activeConversationId: ref('chat'), uploadedPrompts: ref([]), activeTask: ref(null), activeNavigation: ref('plan'),
    storyWorkbench: { state: { view: null }, confirmed: ref(false) } }
  const current = new Function('state', `const {${Object.keys(state).join(',')}}=state; ${compile(selector + steps)}; return productionSteps`)(state)
  assert.equal(current.value[1].status, 'todo')
  assert.equal(current.value[1].hint, '确认后整理提示词')
  state.shotPrompts.value.shot1 = [completePrompt()]
  assert.equal(current.value[1].status, 'done')
  assert.equal(current.value[1].hint, '指令已就绪')
  state.shots.value[0].version++
  assert.equal(current.value[1].status, 'todo')
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
    shotErrors: ref({}), promptReadErrors: ref({}), promptPreview: ref(null),
    async compileComicPrompt(...args) { calls.push(args); await new Promise(resolve => { release = resolve }); return { artifact_id: 'prompt' } },
    async refresh() {}, async loadPrompts() {}, failureText: String,
  }
  const prepare = new Function('state', `const {${Object.keys(state).join(',')}}=state; const conversationEpoch=1,workSelectionEpoch=0; ${code}; return compilePrompt`)(state)
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
  const state = { copiedPrompt, error, promptCopyFeedback: ref(''), copyingPrompt: ref(''), promptCopyError: ref(''), promptPreview: ref(null),
    imagePromptReferences, async copyPromptDelivery(prompt) { await navigator.clipboard.writeText(finalImagePrompt(prompt)); return 'text' },
    getArtifactContentBlob() { throw new Error('text-only prompt must not load media') }, readPromptReference }
  const copy = new Function('state', `const {${Object.keys(state).join(',')}}=state; const conversationEpoch=1,workSelectionEpoch=1,disposed=false; const failureText = String; ${source}; return copyImagePrompt`)(state)
  const prompt = { artifact_id: 'revision2', positive_prompt: '人物：黑发少女\n世界：夜晚山巅', negative_prompt: '文字水印' }
  await copy(prompt)
  assert.equal(writes[0], finalImagePrompt(prompt))
  assert.ok(writes[0].includes('Negative Constraint') && writes[0].includes('黑发少女'))
  assert.equal(copiedPrompt.value, 'revision2')
  navigator.clipboard.writeText = async () => { throw new Error('permission denied') }
  await copy(prompt)
  assert.match(error.value, /无法复制 Prompt.*permission denied/)
  navigator.clipboard.writeText = async value => { writes.push(value) }
  await copy(prompt, 'text')
  assert.equal(error.value, '')
  error.value = 'another business failure'
  await copy(prompt)
  assert.equal(error.value, 'another business failure')
})

const referenceBytes = Buffer.from('verified offline PNG source')
const referencePrompt = (mime = 'image/png') => ({ ...completePrompt(), final_prompt: '保持参考图人物，<不要重画身份>\n禁止内容：水印',
  reference_attachments: [{ artifact_id: 'adopted-original', filename: mime === 'image/png' ? 'original.png' : 'original.webp',
    sha256: createHash('sha256').update(referenceBytes).digest('hex'), image: { image_id: 'image', version: 2 } }] })

test('prompt reference previews verify their own fingerprint and never reuse an unchecked artifact preview', async () => {
  const workspace = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const source = compile(workspace.slice(workspace.indexOf('function promptReferenceKey('), workspace.indexOf('async function refresh(')))
  const state = { referenceUrls: ref({ 'adopted-original': 'blob:unchecked' }), referenceErrors: ref({}), referenceLoading: new Set(),
    readPromptReference, getArtifactContentBlob: async () => new Blob([referenceBytes], { type: 'image/png' }) }
  const open = new Function('state', `const {${Object.keys(state).join(',')}}=state; const disposed=false,failureText=String; ${source}; return openPromptReference`)(state)
  const reference = imagePromptReferences(referencePrompt())[0], key = `${reference.artifact_id}:${reference.sha256}`
  await open(reference)
  assert.match(state.referenceUrls.value[key], /^blob:/)
  assert.notEqual(state.referenceUrls.value[key], 'blob:unchecked')
  const wrong = { ...reference, sha256: 'a'.repeat(64) }, wrongKey = `${wrong.artifact_id}:${wrong.sha256}`
  await open(wrong)
  assert.equal(state.referenceUrls.value[wrongKey], undefined)
  assert.match(state.referenceErrors.value[wrongKey], /内容与指令记录不一致/)
  URL.revokeObjectURL(state.referenceUrls.value[key])
})

test('late reference download failures cannot overwrite a newly selected work', async () => {
  const workspace = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const source = compile(workspace.slice(workspace.indexOf('async function copyImagePrompt('), workspace.indexOf('function exportImagePrompt(')))
  let rejectRead
  const read = new Promise((_resolve, reject) => { rejectRead = reject })
  const state = { error: ref(''), imagePromptReferences, readPromptReference: () => read,
    getArtifactContentBlob() { assert.fail('injected read owns this call') } }
  const handlers = new Function('state', `const {${Object.keys(state).join(',')}}=state; let conversationEpoch=1,workSelectionEpoch=1; const disposed=false,failureText=String; ${source}; return {downloadPromptReference, switchWork(){ workSelectionEpoch++ }}`)(state)
  const prompt = referencePrompt(), pending = handlers.downloadPromptReference(prompt, imagePromptReferences(prompt)[0])
  handlers.switchWork(); state.error.value = 'new work failure'
  rejectRead(new Error('old work read failed')); await pending
  assert.equal(state.error.value, 'new work failure')
})

async function withClipboard(callback) {
  const names = ['navigator', 'ClipboardItem', 'FileReader', 'createImageBitmap', 'document']
  const previous = new Map(names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]))
  const writes = [], texts = [], decoded = []
  class Item {
    constructor(data) { this.data = data; this.types = Object.keys(data) }
    async getType(type) { return this.data[type] }
  }
  class Reader {
    readAsDataURL(content) { content.arrayBuffer().then(bytes => {
      this.result = `data:${content.type};base64,${Buffer.from(bytes).toString('base64')}`
      this.onload?.()
    }).catch(() => this.onerror?.()) }
  }
  const navigator = { clipboard: {
    async write(items) { const resolved = {}; for (const type of items[0].types) resolved[type] = await items[0].getType(type); writes.push(resolved) },
    async writeText(text) { texts.push(text) },
  } }
  for (const [name, value] of Object.entries({ navigator, ClipboardItem: Item, FileReader: Reader,
    async createImageBitmap(blob) { decoded.push(blob.type); return { width: 5, height: 7, close() { decoded.push('closed') } } },
    document: { createElement() { return { getContext() { return { drawImage() {} } }, toBlob(resolve) { resolve(new Blob(['converted PNG'], { type: 'image/png' })) } } } },
  })) Object.defineProperty(globalThis, name, { value, configurable: true })
  try { await callback({ writes, texts, decoded, navigator }) }
  finally { for (const [name, descriptor] of previous) { if (descriptor) Object.defineProperty(globalThis, name, descriptor); else delete globalThis[name] } }
}

test('reference delivery writes actual verified image bytes and complete prompt formats, without public URLs', async () => {
  await withClipboard(async ({ writes, texts }) => {
    const prompt = referencePrompt(), reads = []
    const result = await copyPromptDelivery(prompt, async id => { reads.push(id); return new Blob([referenceBytes], { type: 'image/png' }) })
    assert.equal(result, 'combined')
    assert.deepEqual(reads, ['adopted-original'])
    assert.equal(texts.length, 0)
    assert.deepEqual(Object.keys(writes[0]), ['image/png', 'text/plain', 'text/html'])
    assert.deepEqual(Buffer.from(await writes[0]['image/png'].arrayBuffer()), referenceBytes)
    assert.equal(await writes[0]['text/plain'].text(), finalImagePrompt(prompt))
    const html = await writes[0]['text/html'].text()
    assert.match(html, /data:image\/png;base64,/)
    assert.match(html, /&lt;不要重画身份&gt;/)
    assert.doesNotMatch(html, /https?:|blob:/)
  })
})

test('copying only the original image, explicit text fallback, and prompts without references stay independent', async () => {
  await withClipboard(async ({ writes, texts }) => {
    const prompt = referencePrompt(), load = async () => new Blob([referenceBytes], { type: 'image/png' })
    assert.equal(await copyPromptDelivery(prompt, load, 'image', imagePromptReferences(prompt)[0]), 'image')
    assert.deepEqual(Object.keys(writes[0]), ['image/png'])
    assert.equal(await copyPromptDelivery(prompt, () => assert.fail('text must not reload images'), 'text'), 'text')
    assert.equal(texts[0], finalImagePrompt(prompt))
    assert.equal(await copyPromptDelivery(completePrompt(), () => assert.fail('no attachments')), 'text')
    assert.match(texts[1], /Negative Constraint/)
  })
})

test('non-PNG references are verified before clipboard conversion and their decoded bitmap is closed', async () => {
  for (const mime of ['image/jpeg', 'image/webp']) await withClipboard(async ({ writes, decoded }) => {
    await copyPromptDelivery(referencePrompt(mime), async () => new Blob([referenceBytes], { type: mime }), 'image')
    assert.deepEqual(decoded, [mime, 'closed'])
    assert.equal(writes[0]['image/png'].type, 'image/png')
  })
})

test('missing, changed, foreign and multiple references cannot silently become a successful text-only copy', async () => {
  await withClipboard(async ({ writes, texts, navigator }) => {
    const prompt = referencePrompt()
    await assert.rejects(copyPromptDelivery(prompt, async () => null), /参考图暂时无法读取/)
    await assert.rejects(copyPromptDelivery(prompt, async () => new Blob(['different'], { type: 'image/png' })), /内容与指令记录不一致/)
    await assert.rejects(copyPromptDelivery(prompt, async () => { throw new Error('ERR-content-missing') }), /ERR-content-missing/)
    await assert.rejects(copyPromptDelivery(prompt, async () => assert.fail('foreign reference'), 'image', { ...imagePromptReferences(prompt)[0], artifact_id: 'foreign' }), /不属于/)
    prompt.reference_attachments.push({ ...prompt.reference_attachments[0], artifact_id: 'another', filename: 'second.png' })
    await assert.rejects(copyPromptDelivery(prompt, async () => assert.fail('do not choose first image')), /多张参考图/)
    assert.equal(writes.length, 0); assert.equal(texts.length, 0)
    navigator.clipboard.write = undefined
    await assert.rejects(copyPromptDelivery(referencePrompt(), async () => assert.fail('unsupported clipboard')), /不支持复制图片/)
    assert.equal(await copyPromptDelivery(referencePrompt(), async () => assert.fail(), 'text'), 'text')
  })
})

test('reference UI offers explicit image and text delivery instead of only a filename in the prompt', async () => {
  const html = await renderToString(createSSRApp(ShotView, { run: null, imageMode: 'external', disabled: false, externalPrompt: referencePrompt(),
    copyFeedback: '图文已复制，粘贴后请核对图片与正文' }))
  for (const label of ['复制图文', '仅复制文字', '查看参考图', '粘贴后请核对']) assert.ok(html.includes(label))
  const workspace = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  assert.match(workspace, /aria-label="本次生成需要的参考图"/)
  assert.match(workspace, /复制这张参考图/)
  assert.match(workspace, /下载原图/)
  assert.doesNotMatch(workspace, /外部已附图|已自动上传到/)
})

function compileState(overrides = {}) {
  const ref = value => ({ value })
  return {
    project: ref({ project: { project_id: 'project', current_version: 3 } }), spec: ref({ version: 3 }),
    manualDirectorApproval: ref(true), confirmed: ref(true), compiling: ref(false), busy: ref(false), hasRunning: ref(false),
    shots: ref([{ shot_id: 'shot1', version: 2 }]), selectedShot: ref('shot1'),
    boards: ref([{ storyboard_id: 'board', director_spec_version: 3 }]), selectedBoard: ref('board'),
    preparingShot: ref(''), shotPrompts: ref({}), error: ref(''), refreshConnectionError: ref(''), activeConversationId: ref('conversation'),
    shotErrors: ref({}), promptReadErrors: ref({}), promptPreview: ref(null),
    async refresh() {}, async loadPrompts() {}, failureText: String, ...overrides,
  }
}
function workspaceHandler(name, nextName, state) {
  const workspace = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const body = compile(workspace.slice(workspace.indexOf(`async function ${name}(`), workspace.indexOf(`async function ${nextName}(`)))
  return new Function('state', `const {${Object.keys(state).filter(key => key !== name).join(',')}}=state; const conversationEpoch=1, workSelectionEpoch=1; let disposed=false; ${body}; return ${name}`)(state)
}

test('a compile failure stays in its shot, preserves the saved prompt, and only an explicit retry submits again', async () => {
  const saved = completePrompt()
  let calls = 0
  const state = compileState({ async compileComicPrompt() { calls++; throw new Error('主模型连接失败 · ERR-offline') } })
  state.shotPrompts.value.shot1 = [saved]
  state.promptPreview.value = saved
  const prepare = workspaceHandler('compilePrompt', 'viewImagePrompt', state)
  await prepare()
  assert.equal(calls, 1)
  assert.equal(state.error.value, '')
  assert.match(state.shotErrors.value.shot1, /ERR-offline/)
  assert.equal(state.shotPrompts.value.shot1[0], saved)
  assert.equal(state.promptPreview.value, saved)
  assert.equal(state.compiling.value, false)
  assert.equal(state.preparingShot.value, '')
  await prepare()
  assert.equal(calls, 2)
})

test('a saved compile result survives refresh failure and does not open over another selected shot', async () => {
  let release
  const saved = completePrompt({ version: 2, artifact_id: 'saved-new' })
  const state = compileState({
    async compileComicPrompt() { return new Promise(resolve => { release = resolve }) },
    async refresh() { throw new Error('读取连接中断') },
    async loadPrompts() { throw new Error('another selected shot must not be loaded') },
  })
  const prepare = workspaceHandler('compilePrompt', 'viewImagePrompt', state)
  const pending = prepare()
  state.selectedShot.value = 'shot2'
  state.promptPreview.value = { artifact_id: 'shot2-existing' }
  release(saved)
  await pending
  assert.equal(state.shotPrompts.value.shot1[0], saved)
  assert.equal(state.promptPreview.value.artifact_id, 'shot2-existing')
  assert.equal(state.shotErrors.value.shot1, '')
  assert.equal(state.error.value, '')
  assert.match(state.refreshConnectionError.value, /读取连接中断/)
  assert.equal(state.compiling.value, false)
})

test('cancelled and failed prompt reads preserve existing saved results, and late reads respect selected shot', async () => {
  for (const outcome of ['cancelled', 'failed', 'late']) {
    const saved = completePrompt()
    let release
    const state = compileState({ prompts: { value: [saved] }, compareVersions: { value: [] },
      async getComicPromptVersions() {
        if (outcome === 'failed') throw new Error('读取失败')
        if (outcome === 'cancelled') return null
        return new Promise(resolve => { release = resolve })
      } })
    state.shotPrompts.value.shot1 = [saved]
    const load = workspaceHandler('loadPrompts', 'compilePrompt', state)
    const pending = load()
    if (outcome === 'late') {
      state.selectedShot.value = 'shot2'
      state.prompts.value = [{ artifact_id: 'shot2-existing', shot_id: 'shot2' }]
      release([completePrompt({ artifact_id: 'late-shot1' })])
    }
    await pending
    assert.equal(state.shotPrompts.value.shot1[0], saved)
    assert.equal(state.prompts.value[0].artifact_id, outcome === 'late' ? 'shot2-existing' : saved.artifact_id)
    assert.equal(state.error.value, '')
    if (outcome === 'failed') assert.match(state.promptReadErrors.value.shot1, /读取失败/)
    assert.equal(state.refreshConnectionError.value, '')
  }
})

test('one failed shot history does not block another prompt and cancelled history keeps its previous version', async () => {
  const saved = completePrompt()
  const state = compileState({
    async getComicShots() { return [{ shot_id: 'shot1' }, { shot_id: 'shot2' }, { shot_id: 'shot3' }] },
    async getComicPromptVersions(id) {
      if (id === 'shot1') throw new Error('镜头 1 读取失败')
      return id === 'shot2' ? [completePrompt({ shot_id: 'shot2' })] : null
    },
    uploadedArtifact: () => '', async openReference() {},
  })
  state.shotPrompts.value.shot3 = [saved]
  const load = workspaceHandler('loadShots', 'loadPrompts', state)
  await load()
  assert.match(state.promptReadErrors.value.shot1, /镜头 1 读取失败/)
  assert.equal(state.shotErrors.value.shot1, undefined)
  assert.equal(state.shotPrompts.value.shot2[0].shot_id, 'shot2')
  assert.equal(state.shotPrompts.value.shot3[0], saved)
  assert.equal(state.shots.value.length, 3)
  assert.equal(state.error.value, '')
})

async function renderWorkspaceShotError(state, shotId = 'shot1') {
  const workspace = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const binding = workspace.match(/:prompt-error="([^"]+)"/)?.[1]
  assert.ok(binding, 'the shot error must be bound to the actual shot component')
  const promptError = new Function('shot', 'shotErrors', 'promptReadErrors', `return ${binding}`)(
    { shot_id: shotId }, state.shotErrors.value, state.promptReadErrors.value)
  return renderToString(createSSRApp(ShotView, { run: null, imageMode: 'external', disabled: false, promptError }))
}

test('shot history recovers without erasing a real compile failure or reporting an absent prompt ready', async () => {
  let readable = false
  const state = compileState({
    async getComicShots() { return [{ shot_id: 'shot1', version: 2 }] },
    async getComicPromptVersions() { if (!readable) throw new Error('历史读取暂时失败'); return [] },
    async compileComicPrompt() { throw new Error('主模型连接失败 · ERR-compile') },
    uploadedArtifact: () => '', async openReference() {},
  })
  const load = workspaceHandler('loadShots', 'loadPrompts', state)
  await load()
  assert.match(state.promptReadErrors.value.shot1, /历史读取暂时失败/)
  let html = await renderWorkspaceShotError(state)
  assert.equal((html.match(/role="alert"/g) ?? []).length, 1)
  assert.match(html, /历史读取暂时失败/)
  await workspaceHandler('compilePrompt', 'viewImagePrompt', state)()
  const compileError = state.shotErrors.value.shot1
  html = await renderWorkspaceShotError(state)
  assert.equal((html.match(/role="alert"/g) ?? []).length, 1)
  assert.match(html, /ERR-compile/)
  assert.doesNotMatch(html, /历史读取暂时失败/)
  readable = true
  await load()
  assert.equal(state.promptReadErrors.value.shot1, '')
  assert.equal(state.shotErrors.value.shot1, compileError)
  assert.deepEqual(state.shotPrompts.value.shot1, [])
  html = await renderWorkspaceShotError(state)
  assert.match(html, /ERR-compile/)
  assert.match(html, /data-media-status="prompt_pending"/)
  assert.doesNotMatch(html, /Prompt 已生成|等待外部图片|历史读取暂时失败/)
  assert.equal(state.error.value, '')
})

test('selected prompt read clears only its recovered diagnostic and a cancelled read preserves it', async () => {
  let outcome = 'failed'
  const state = compileState({ prompts: ref([]), compareVersions: ref([]),
    async getComicPromptVersions() {
      if (outcome === 'failed') throw new Error('当前镜头历史读取失败')
      return outcome === 'cancelled' ? null : []
    } })
  state.shotErrors.value.shot1 = '图片上传失败 · ERR-upload'
  state.promptReadErrors.value.shot2 = '另一个镜头读取失败'
  const load = workspaceHandler('loadPrompts', 'compilePrompt', state)
  await load()
  assert.match(state.promptReadErrors.value.shot1, /当前镜头历史读取失败/)
  outcome = 'cancelled'
  await load()
  assert.match(state.promptReadErrors.value.shot1, /当前镜头历史读取失败/)
  outcome = 'success'
  await load()
  assert.equal(state.promptReadErrors.value.shot1, '')
  assert.equal(state.promptReadErrors.value.shot2, '另一个镜头读取失败')
  assert.equal(state.shotErrors.value.shot1, '图片上传失败 · ERR-upload')
  const html = await renderWorkspaceShotError(state)
  assert.equal((html.match(/role="alert"/g) ?? []).length, 1)
  assert.match(html, /ERR-upload/)
  assert.doesNotMatch(html, /当前镜头历史读取失败|另一个镜头读取失败|Prompt 已生成/)
  assert.equal(state.error.value, '')
  assert.equal(state.refreshConnectionError.value, '')
})

test('late history success and failure cannot alter another selected work or shot diagnostics', async () => {
  for (const reader of ['loadShots', 'loadPrompts']) {
    for (const rejected of [false, true]) {
      let resolve, reject
      const state = compileState({ prompts: ref([]), compareVersions: ref([]),
        async getComicShots() { return [{ shot_id: 'shot1' }] },
        async getComicPromptVersions() { return new Promise((yes, no) => { resolve = yes; reject = no }) },
        uploadedArtifact: () => '', async openReference() {},
      })
      const pending = workspaceHandler(reader, reader === 'loadShots' ? 'loadPrompts' : 'compilePrompt', state)()
      await Promise.resolve()
      state.project.value = { project: { project_id: 'other-project', current_version: 1 } }
      state.selectedShot.value = 'other-shot'
      state.shotPrompts.value = { 'other-shot': [{ artifact_id: 'other-prompt' }] }
      state.promptReadErrors.value = { 'other-shot': '新作品读取诊断' }
      state.shotErrors.value = { 'other-shot': '新作品操作失败' }
      if (rejected) reject(new Error('旧作品迟到失败'))
      else resolve([])
      await pending
      assert.deepEqual(state.promptReadErrors.value, { 'other-shot': '新作品读取诊断' })
      assert.deepEqual(state.shotErrors.value, { 'other-shot': '新作品操作失败' })
      assert.deepEqual(state.shotPrompts.value, { 'other-shot': [{ artifact_id: 'other-prompt' }] })
      assert.equal(state.error.value, '')
    }
  }
})

test('a cancelled shot-list refresh keeps the selected shot and its saved prompt', async () => {
  const saved = completePrompt()
  const state = compileState({ async getComicShots() { return null } })
  state.shotPrompts.value.shot1 = [saved]
  state.promptReadErrors.value.shot1 = '尚未恢复的历史读取问题'
  await workspaceHandler('loadShots', 'loadPrompts', state)()
  assert.equal(state.selectedShot.value, 'shot1')
  assert.equal(state.shots.value.length, 1)
  assert.equal(state.shotPrompts.value.shot1[0], saved)
  assert.equal(state.promptReadErrors.value.shot1, '尚未恢复的历史读取问题')
})

test('prompt API cancellation is distinct from a successfully empty list', async () => {
  const service = readFileSync(new URL('../src/services/core.ts', import.meta.url), 'utf8')
  const first = service.slice(service.indexOf('export async function getComicPromptVersions('), service.indexOf('export function compileComicPrompt('))
  const second = service.slice(service.indexOf('export async function getProjectPrompts('), service.indexOf('export function confirmComicPrompt('))
  const boards = service.slice(service.indexOf('export async function getComicStoryboards('), service.indexOf('export function createComicStoryboard('))
  const shots = service.slice(service.indexOf('export async function getComicShots('), service.indexOf('export async function getDirectorVersions('))
  for (const result of [null, { versions: [], prompts: [], storyboards: [], shots: [] }]) {
    const api = new Function('coreGet', `${compile((first + second + boards + shots).replaceAll('export ', ''))}; return { getComicPromptVersions, getProjectPrompts, getComicStoryboards, getComicShots }`)(async () => result)
    assert.deepEqual(await api.getComicPromptVersions('shot1'), result === null ? null : [])
    assert.deepEqual(await api.getProjectPrompts('project'), result === null ? null : [])
    assert.deepEqual(await api.getComicStoryboards('project'), result === null ? null : [])
    assert.deepEqual(await api.getComicShots('board'), result === null ? null : [])
  }
})
