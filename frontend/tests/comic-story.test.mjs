import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createSSRApp, h, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'

const transpile = source => ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const geometryUrl = moduleUrl(transpile(readFileSync(new URL('../src/components/ui/imageViewport.ts', import.meta.url), 'utf8')))
const viewerUrl = moduleUrl(transpile(compileScript(parse(readFileSync(new URL('../src/components/ui/ImageViewport.vue', import.meta.url), 'utf8')).descriptor,
  { id: 'image-viewer', inlineTemplate: true }).content).replace(/from (["'])vue\1/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`)
  .replace(/from (["'])\.\/imageViewport\1/g, `from ${JSON.stringify(geometryUrl)}`))
const coreUrl = moduleUrl(`export class CoreApiError extends Error { constructor(message, httpStatus) { super(message); this.httpStatus=httpStatus } }`)
const controllerUrl = moduleUrl(transpile(readFileSync(new URL('../src/domains/comic/storyWorkbench.ts', import.meta.url), 'utf8'))
  .replace(/from (["'])vue\1/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`)
  .replace(/from (["'])\.\.\/\.\.\/services\/core\1/g, `from ${JSON.stringify(coreUrl)}`))
const { createStoryWorkbench, storyImageKey } = await import(controllerUrl)
const { CoreApiError } = await import(coreUrl)
const viewUrl = moduleUrl(transpile(compileScript(parse(readFileSync(new URL('../src/components/comic/ComicFlowStage.vue', import.meta.url), 'utf8')).descriptor,
  { id: 'comic-story', inlineTemplate: true }).content)
  .replace(/from (["'])vue\1/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`)
  .replace(/from (["'])lucide-vue-next\1/g, `from ${JSON.stringify(import.meta.resolve('lucide-vue-next'))}`)
  .replace(/from (["'])\.\.\/ui\/ImageViewport.vue\1/g, `from ${JSON.stringify(viewerUrl)}`)
  .replace(/from (["'])\.\.\/\.\.\/domains\/comic\/storyWorkbench\1/g, `from ${JSON.stringify(controllerUrl)}`))
const { default: StoryStage } = await import(viewUrl)
const clone = value => JSON.parse(JSON.stringify(value))
const image = { image_id: 'first', version: 1, artifact_id: 'first-v1', filename: 'horse.png', project_id: 'project-a', conversation_id: 'chat', source_project_title: '废墟战马', sha256: 'first-hash', mime_type: 'image/png' }
const second = { ...image, image_id: 'second', version: 2, artifact_id: 'second-v2', project_id: 'project-b', filename: 'city.webp', source_project_title: '雨中城市', mime_type: 'image/webp' }
const shot = { shot_id: 'stable-shot', image: { image_id: image.image_id, version: image.version }, action: '机械战马缓慢抬头', camera: '缓慢后移', duration_seconds: 4, transition: '切入下一镜' }
const draft = { title: '回到城市', summary: '战马穿过废墟，看见城市', instruction: '保持人物身份，各图独立', shots: [shot] }
const story = { ...draft, version: 1, story_id: 'story-chat', anchor_project_id: 'project-a', content_sha256: 'content-v1', fixed_images: [image], run_id: 'original', created_at: '2026-10-14T01:00:00Z' }
const view = { story, review: { status: 'pass', validation_requested: true, findings: [], content_sha256: 'content-v1' }, confirmation: { confirmed: true, story_version: 1, content_sha256: 'content-v1' }, export_text: '回到城市\n镜头 1 动作 机械战马缓慢抬头\nimages/horse-v1.png', manifest: { story_id: 'story-chat', version: 1, content_sha256: 'content-v1', images: [], shots: [{ shot_id: shot.shot_id, sequence_number: 1, image_id: image.image_id, image_version: 1, filename: 'horse-v1.png', duration_seconds: 4, transition: shot.transition }] }, exports: [], missing_images: [], project_version: 10 }
const flush = async () => { for (let i = 0; i < 35; i++) { await Promise.resolve(); await nextTick() } }
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
function fixture(initial = view) {
  let current = clone(initial), tasks = [], versions = initial.story ? [clone(initial.story)] : []
  const calls = []
  const api = {
    getComicStory: async owner => { calls.push(['get', owner]); return clone(current) },
    getComicStoryImages: async () => ({ images: [clone(image), clone(second)] }),
    getComicStoryVersions: async () => clone(versions), getComicStoryRequests: async () => clone(tasks),
    getComicProject: async id => { calls.push(['project', id]); return { project: { project_id: id, current_version: 10 } } },
    getArtifactContentUrl: async id => { calls.push(['preview', id]); return 'blob:' + id },
    saveComicStory: async (owner, body) => {
      calls.push(['save', clone(body), owner]); const version = body.expected_story_version + 1
      current = { ...current, story: { ...current.story, ...clone(body.draft), version, anchor_project_id: body.anchor_project_id, content_sha256: 'content-v' + version, fixed_images: [image, second] }, review: null, confirmation: null, exports: [] }
      versions.unshift(clone(current.story)); return clone(current)
    },
    generateComicStory: async (owner, body) => { calls.push(['generate', clone(body), owner]); return clone(current) },
    reviewComicStory: async (owner, body) => { calls.push(['review', clone(body), owner]); current.review = { status: 'pass', validation_requested: true, findings: [] }; return clone(current) },
    confirmComicStory: async (owner, body) => { calls.push(['confirm', clone(body), owner]); current.confirmation = { confirmed: true }; return clone(current) },
    restoreComicStory: async (owner, body) => { calls.push(['restore', clone(body), owner]); current.story.version++; current.confirmation = null; return clone(current) },
    adoptComicStoryCandidate: async (owner, body) => { calls.push(['candidate', clone(body), owner]); current.story.version++; return clone(current) },
    exportComicStory: async (owner, body) => { calls.push(['export', clone(body), owner]); current.exports.unshift({ artifact_id: 'zip', filename: 'story-v1.zip', story_version: current.story.version }); return clone(current) },
  }
  const controller = createStoryWorkbench(() => ({ project_id: 'project-b', current_version: 7 }), api)
  return { controller, api, calls, setView: value => { current = clone(value) }, setTasks: value => { tasks = value } }
}

test('same chat reads adopted images from multiple works and keeps original story versions after candidates change', async () => {
  const f = fixture(); await f.controller.load('chat')
  try {
    assert.deepEqual(f.controller.state.images.map(item => item.project_id), ['project-a', 'project-b'])
    assert.equal(f.controller.state.draft.shots[0].image.version, 1)
    f.api.getComicStoryImages = async () => ({ images: [{ ...image, version: 9 }, second] })
    await f.controller.load()
    assert.equal(f.controller.state.draft.shots[0].image.version, 1)
    assert.equal(f.controller.confirmed.value, true)
  } finally { await f.controller.dispose() }
})

test('repeated use, sorting and replacement preserve stable shot identity and save immutable references', async () => {
  const f = fixture(); await f.controller.load('chat')
  try {
    f.controller.addImage(image); f.controller.addImage(second)
    const ids = f.controller.state.draft.shots.map(item => item.shot_id)
    assert.equal(new Set(ids).size, 3)
    f.controller.moveShot(2, 0); f.controller.replaceImage(0, storyImageKey(image)); await f.controller.save()
    const saved = f.calls.find(call => call[0] === 'save')[1]
    assert.deepEqual(saved.draft.shots.map(item => item.shot_id), [ids[2], ids[0], ids[1]])
    assert.deepEqual(saved.draft.shots[0].image, { image_id: 'first', version: 1 })
    assert.equal(saved.anchor_project_id, 'project-a')
    assert.equal(saved.expected_project_version, 10)
    assert.equal(saved.expected_story_version, 1)
    assert.equal(f.controller.confirmed.value, false)
    f.controller.removeShot(1); assert.equal(f.controller.state.draft.shots[1].shot_id, ids[1])
  } finally { await f.controller.dispose() }
})

test('auto-save persists changes without a model call and another controller restores them', async () => {
  const f = fixture(); await f.controller.load('chat')
  f.controller.state.draft.title = '人工标题'; f.controller.changed()
  await new Promise(resolve => setTimeout(resolve, 700)); await flush()
  const reopened = createStoryWorkbench(() => null, f.api)
  try {
    await reopened.load('chat'); assert.equal(reopened.state.draft.title, '人工标题')
    assert.equal(f.calls.filter(call => call[0] === 'generate' || call[0] === 'review').length, 0)
    assert.equal(reopened.confirmed.value, false)
  } finally { await f.controller.dispose(); await reopened.dispose() }
})

test('edits made during saving remain visible and save as the next revision', async () => {
  const f = fixture(), pending = deferred(), firstSave = f.api.saveComicStory
  let once = true
  f.api.saveComicStory = async (...args) => { if (once) { once = false; await pending.promise }; return firstSave(...args) }
  await f.controller.load('chat'); f.controller.state.draft.title = '第一次修改'; f.controller.changed()
  const saving = f.controller.save(); await flush()
  f.controller.state.draft.title = '第二次修改'; f.controller.changed(); pending.resolve(); await saving
  try {
    assert.equal(f.controller.state.draft.title, '第二次修改')
    const saves = f.calls.filter(call => call[0] === 'save')
    assert.deepEqual(saves.map(call => call[1].draft.title), ['第一次修改', '第二次修改'])
    assert.deepEqual(saves.map(call => call[1].expected_story_version), [1, 2])
  } finally { await f.controller.dispose() }
})

test('AI keeps framework images alongside explicit selections and a late result never overwrites manual text', async () => {
  const f = fixture(), pending = deferred()
  f.api.generateComicStory = async (_owner, body) => { f.calls.push(['generate', clone(body)]); return pending.promise }
  await f.controller.load('chat'); f.controller.toggleImage(second)
  const generating = f.controller.mutate('generate'); await flush()
  f.controller.state.draft.title = '等待时人工修改'; f.controller.changed()
  pending.resolve({ ...clone(view), story: { ...clone(story), title: '晚到 AI 标题', version: 2 } }); await generating; await flush()
  try {
    assert.deepEqual(f.calls.find(call => call[0] === 'generate')[1].images, [{ image_id: 'first', version: 1 }, { image_id: 'second', version: 2 }])
    assert.equal(f.controller.state.draft.title, '等待时人工修改')
    assert.ok(f.calls.some(call => call[0] === 'save' && call[1].draft.title === '等待时人工修改'))
  } finally { await f.controller.dispose() }
})

test('network uncertainty queries the original request and never repeats a paid call', async () => {
  const f = fixture()
  f.api.generateComicStory = async (_owner, body) => { f.calls.push(['generate', clone(body)]); throw new TypeError('connection lost') }
  await f.controller.load('chat'); f.controller.toggleImage(image)
  await f.controller.mutate('generate')
  const id = f.controller.state.unknownRequest.request_id
  await f.controller.mutate('generate'); assert.equal(f.calls.filter(call => call[0] === 'generate').length, 1)
  f.setTasks([{ id: 'paid-run', status: 'running', state: { media_input: { request_id: id } } }]); await f.controller.recoverRequest()
  assert.equal(f.controller.state.unknownRequest.request_id, id)
  f.setTasks([{ id: 'paid-run', status: 'completed', state: { media_input: { request_id: id }, story_result: view } }]); await f.controller.recoverRequest()
  try { assert.equal(f.controller.state.unknownRequest, null); assert.equal(f.calls.filter(call => call[0] === 'generate').length, 1) }
  finally { await f.controller.dispose() }
})

test('local validation and source lookup failures never become an unknown model submission', async () => {
  const f = fixture(); await f.controller.load('chat'); f.controller.toggleImage(image)
  try {
    const saved = f.controller.state.draft
    // 既没有要求也没有已填框架：本地就该拦住，不发请求。
    f.controller.state.draft = { title: '', summary: '', instruction: '', shots: [] }
    assert.equal(await f.controller.mutate('generate'), false)
    assert.equal(f.controller.state.unknownRequest, null)
    assert.equal(f.calls.some(call => call[0] === 'generate'), false)
    f.controller.state.draft = saved
    f.controller.state.draft.instruction = draft.instruction
    f.api.getComicProject = async () => { throw new TypeError('source lookup unavailable') }
    assert.equal(await f.controller.mutate('generate'), false)
    assert.equal(f.controller.state.unknownRequest, null)
  } finally { await f.controller.dispose() }
})

test('AI draft reuses the filled framework: no checkbox and no instruction needed', async () => {
  const f = fixture(); await f.controller.load('chat')
  try {
    // 只有已添加的镜头（未勾选任何图片、要求为空）也必须能整理初稿。
    f.controller.state.draft.title = '橙留香独自面对敌军'
    f.controller.state.draft.instruction = ''
    f.controller.changed()
    assert.equal(await f.controller.mutate('generate'), true)
    const call = f.calls.find(item => item[0] === 'generate')[1]
    assert.deepEqual(call.images, [{ image_id: image.image_id, version: image.version }])
    assert.equal(call.instruction, '')
    assert.equal(call.draft.title, '橙留香独自面对敌军')
    assert.equal(call.draft.shots[0].shot_id, shot.shot_id)
  } finally { await f.controller.dispose() }
})

test('generated internal review cannot confirm until the user validates, and every edit locks it again', async () => {
  const internal = { ...clone(view), review: { ...clone(view.review), validation_requested: false }, confirmation: null }
  const f = fixture(internal); await f.controller.load('chat')
  const render = () => renderToString(createSSRApp({ render: () => h(StoryStage, { stage: 'story', workbench: f.controller }) }))
  try {
    assert.equal(f.controller.canConfirm.value, false)
    assert.match(await render(), /初稿已完成内部检查/)
    assert.match(await render(), /<button[^>]*disabled[^>]*>确认当前故事<\/button>/)
    assert.equal(await f.controller.mutate('confirm'), false)
    assert.equal(f.calls.some(call => call[0] === 'confirm'), false)
    await f.controller.mutate('review')
    assert.equal(f.controller.canConfirm.value, true)
    await f.controller.mutate('confirm')
    assert.equal(f.controller.canExport.value, true)
    f.controller.state.draft.shots[0].camera = '修改运镜'; f.controller.changed()
    assert.equal(f.controller.canConfirm.value, false)
    assert.equal(f.controller.canExport.value, false)
    assert.equal(await f.controller.mutate('confirm'), false)
    assert.equal(f.calls.filter(call => call[0] === 'confirm').length, 1)
    await f.controller.mutate('review'); await f.controller.mutate('confirm')
    assert.equal(f.controller.canExport.value, true)
  } finally { await f.controller.dispose() }
})

test('requirements alone generate a draft with selected images and no prefilled fields', async () => {
  const f = fixture({ ...clone(view), story: null, review: null, confirmation: null })
  await f.controller.load('chat')
  try {
    f.controller.state.draft.instruction = '起承转合：发现旧信，启程寻找朋友，最后重逢。'
    f.controller.changed(); f.controller.toggleImage(second)
    assert.equal(await f.controller.mutate('generate'), true)
    const body = f.calls.find(call => call[0] === 'generate')[1]
    assert.equal(body.draft.shots.length, 0)
    assert.match(body.instruction, /最后重逢/)
    assert.deepEqual(body.images, [{ image_id: 'second', version: 2 }])
  } finally { await f.controller.dispose() }
})

test('advisory-only validation allows direct confirmation while preserving acceptance in the API', async () => {
  const review = { status: 'warning', validation_requested: true, findings: [{ severity: 'warning',
    shot_id: shot.shot_id, field: 'camera', code: 'continuity', message: '可进一步平滑运镜。',
    evidence: shot.camera, suggestion: '适当减缓移动速度。' }] }
  const f = fixture({ ...clone(view), review, confirmation: null }); await f.controller.load('chat')
  try {
    for (const stage of ['story', 'export']) {
      const html = await renderToString(createSSRApp({ render: () => h(StoryStage, { stage, workbench: f.controller }) }))
      assert.match(html, /校验通过/)
      assert.doesNotMatch(html, /<button[^>]*disabled[^>]*>确认当前故事<\/button>/)
      assert.doesNotMatch(html, /type="checkbox"[^>]*>\s*已查看并接受/)
    }
    assert.equal(await f.controller.mutate('confirm'), true)
    assert.equal(f.calls.find(call => call[0] === 'confirm')[1].accept_warnings, true)
    assert.equal(f.controller.canExport.value, true)
  } finally { await f.controller.dispose() }
})

test('generation rejects more than eight combined images before any submit instead of truncating', async () => {
  const f = fixture(); await f.controller.load('chat')
  try {
    for (let i = 0; i < 8; i++) {
      const next = { ...clone(second), image_id: `extra-${i}`, artifact_id: `extra-${i}` }
      f.controller.state.images.push(next); f.controller.toggleImage(next)
    }
    assert.equal(await f.controller.mutate('generate'), false)
    assert.match(f.controller.state.error, /合计最多使用 8/)
    assert.equal(f.calls.some(call => ['generate', 'save'].includes(call[0])), false)
  } finally { await f.controller.dispose() }
})

test('validation findings name the shot, field, error type and fix', async () => {
  const review = { status: 'needs_revision', validation_requested: true, content_sha256: 'content-v1', findings: [
    { severity: 'error', shot_id: shot.shot_id, field: 'action', message: '镜头 1 缺少动作。',
      evidence: '（空）', code: 'missing_action', suggestion: '补充这一镜的动作，写清谁做了什么。' },
  ] }
  const f = fixture({ ...clone(view), review, confirmation: null })
  await f.controller.load('chat')
  try {
    const html = await renderToString(createSSRApp({ render: () => h(StoryStage, { stage: 'story', workbench: f.controller }) }))
    assert.match(html, /镜头 1 · 动作<small>动作缺失/)
    assert.match(html, /镜头 1 缺少动作/)
    assert.match(html, /建议：补充这一镜的动作/)
    assert.ok(html.indexOf('aria-label="故事校验结果"') < html.indexOf('class="flow-columns"'))
    assert.equal(html.match(/镜头 1 缺少动作/g)?.length, 1)
    assert.doesNotMatch(html, /依据：|（空）/)
    assert.equal(f.controller.canExport.value, false)
    f.controller.state.draft.title = '人工修改后等待重新校验'; f.controller.changed()
    const dirtyHtml = await renderToString(createSSRApp({ render: () => h(StoryStage, { stage: 'story', workbench: f.controller }) }))
    assert.doesNotMatch(dirtyHtml, /aria-label="故事校验结果"/)
  } finally { await f.controller.dispose() }
})

test('long historical validation text is collapsed at the top and never repeats source evidence', async () => {
  const message = '该镜头运镜有冲突。'.repeat(30), suggestion = '保留一个连贯的运镜方向。'.repeat(30)
  const f = fixture({ ...clone(view), confirmation: null, review: {
    status: 'needs_revision', validation_requested: true, findings: [
      { severity: 'error', shot_id: shot.shot_id, field: 'camera', code: 'camera_conflict',
        message, suggestion, evidence: '不应展示的长篇原始证据' },
    ],
  } })
  await f.controller.load('chat')
  try {
    const html = await renderToString(createSSRApp({ render: () => h(StoryStage, { stage: 'story', workbench: f.controller }) }))
    assert.match(html, /展开完整说明/)
    assert.doesNotMatch(html, /<details[^>]*\bopen\b/)
    assert.doesNotMatch(html, /不应展示的长篇原始证据|依据：/)
    const collapsed = html.replace(/<details[\s\S]*?<\/details>/g, '')
    assert.ok(collapsed.includes(message.slice(0, 80) + '…'))
    assert.ok(collapsed.includes(suggestion.slice(0, 100) + '…'))
    assert.ok(!collapsed.includes(message))
    assert.ok(html.indexOf('展开完整说明') < html.indexOf('class="flow-columns"'))
    assert.match(html, /href="#story-validation-results"/)
  } finally { await f.controller.dispose() }
})

test('simultaneous generation clicks submit only one request', async () => {
  const f = fixture(), pending = deferred()
  f.api.generateComicStory = async (_owner, body) => { f.calls.push(['generate', body]); return pending.promise }
  await f.controller.load('chat'); f.controller.toggleImage(image)
  const first = f.controller.mutate('generate'), secondClick = f.controller.mutate('generate')
  await flush()
  assert.equal(f.calls.filter(call => call[0] === 'generate').length, 1)
  pending.resolve(clone(view)); await Promise.all([first, secondClick]); await f.controller.dispose()
})

test('generation responds immediately while saving and reports failures once at the top', async () => {
  const f = fixture(), pending = deferred()
  f.api.saveComicStory = async () => { await pending.promise; throw new CoreApiError('镜头 1 的图片原文件缺失', 400) }
  await f.controller.load('chat')
  f.controller.state.draft.title = '待保存的标题'; f.controller.changed()
  const generating = f.controller.mutate('generate')
  try {
    assert.equal(f.controller.state.operation, 'generate')
    await flush()
    const render = () => renderToString(createSSRApp({ render: () => h(StoryStage, { stage: 'story', workbench: f.controller }) }))
    assert.match(await render(), /正在保存并准备初稿/)
    pending.resolve(); assert.equal(await generating, false)
    const html = await render()
    assert.match(html, /镜头 1 的图片原文件缺失/)
    assert.equal(html.match(/镜头 1 的图片原文件缺失/g)?.length, 1)
    assert.ok(html.indexOf('镜头 1 的图片原文件缺失') < html.indexOf('class="flow-columns"'))
    assert.doesNotMatch(html.slice(html.indexOf('class="flow-ai"')), /镜头 1 的图片原文件缺失/)
    assert.equal(f.controller.state.operation, '')
    assert.equal(f.calls.some(call => call[0] === 'generate'), false)
    assert.equal(f.controller.state.draft.title, '待保存的标题')
  } finally { await f.controller.dispose() }
})

test('successful rereading clears the stale error instead of leaving a dead retry message', async () => {
  const f = fixture(); await f.controller.load('chat')
  f.controller.state.error = '先前的图片来源错误'
  try {
    await f.controller.load()
    assert.equal(f.controller.state.error, '')
    assert.equal(f.controller.state.draft.title, draft.title)
  } finally { await f.controller.dispose() }
})

test('generation repairs an unavailable reference only from a selected identical adopted image', async () => {
  const original = { ...clone(image), version: 2, artifact_id: 'deleted-original' }
  const fixedShot = { ...clone(shot), image: { image_id: image.image_id, version: 2 } }
  const f = fixture({ ...clone(view), story: { ...clone(story), shots: [fixedShot], fixed_images: [original] },
    missing_images: [{ shot_id: shot.shot_id, reason: '原记录已在回收站' }], confirmation: null })
  await f.controller.load('chat')
  try {
    assert.equal(f.controller.imageReplacement(f.controller.state.draft.shots[0]).artifact_id, image.artifact_id)
    const html = await renderToString(createSSRApp({ render: () => h(StoryStage, { stage: 'story', workbench: f.controller }) }))
    assert.match(html, /使用已采用的同图修复引用/)
    f.controller.toggleImage(image)
    assert.equal(await f.controller.mutate('generate'), true)
    const saved = f.calls.find(call => call[0] === 'save')[1].draft
    assert.deepEqual(saved.shots[0], shot)
    assert.equal(saved.title, draft.title)
    assert.equal(saved.summary, draft.summary)
    const generated = f.calls.find(call => call[0] === 'generate')[1]
    assert.deepEqual(generated.images, [{ image_id: image.image_id, version: 1 }])
    assert.match(f.controller.state.notice, /修复 1 个镜头引用/)
  } finally { await f.controller.dispose() }
})

test('reference repair never substitutes different pixels or another source owner', async () => {
  const original = { ...clone(image), version: 2, artifact_id: 'deleted-original' }
  const fixedShot = { ...clone(shot), image: { image_id: image.image_id, version: 2 } }
  const f = fixture({ ...clone(view), story: { ...clone(story), shots: [fixedShot], fixed_images: [original] },
    missing_images: [{ shot_id: shot.shot_id, reason: '原图失效' }] })
  await f.controller.load('chat')
  try {
    f.controller.state.images = [{ ...clone(image), sha256: 'different-content' }]
    assert.equal(f.controller.imageReplacement(f.controller.state.draft.shots[0]), undefined)
    f.controller.repairImage(0)
    assert.equal(f.controller.state.draft.shots[0].image.version, 2)
    f.controller.state.images = [{ ...clone(image), project_id: 'foreign-project' }]
    assert.equal(f.controller.imageReplacement(f.controller.state.draft.shots[0]), undefined)
    assert.equal(f.calls.some(call => call[0] === 'save'), false)
  } finally { await f.controller.dispose() }
})

test('manual save immediately exposes its new revision in story history', async () => {
  const f = fixture(); await f.controller.load('chat')
  try {
    f.controller.state.draft.title = '新历史版本'; f.controller.changed(); await f.controller.save()
    assert.equal(f.controller.state.versions[0].version, 2)
    assert.equal(f.controller.state.versions[0].title, '新历史版本')
  } finally { await f.controller.dispose() }
})

test('an anchored empty story keeps its server owner after switching works', async () => {
  const f = fixture({ ...clone(view), story: null, anchor_project_id: 'project-a', review: null, confirmation: null })
  await f.controller.load('chat')
  try {
    f.controller.addImage(image); await f.controller.save()
    const saved = f.calls.find(call => call[0] === 'save')[1]
    assert.equal(saved.anchor_project_id, 'project-a')
    assert.equal(saved.expected_story_version, 0)
  } finally { await f.controller.dispose() }
})

test('CAS conflict preserves local content and explicit resave uses current server version without generating', async () => {
  const f = fixture(), originalSave = f.api.saveComicStory
  f.api.saveComicStory = async () => { throw new CoreApiError('版本已更新', 409) }
  await f.controller.load('chat'); f.controller.state.draft.title = '本地保留'; f.controller.changed()
  f.setView({ ...clone(view), story: { ...clone(story), version: 5, title: '另一处修改' } }); await f.controller.save()
  assert.equal(f.controller.state.draft.title, '本地保留'); assert.equal(f.controller.state.conflict, true)
  f.api.saveComicStory = originalSave; await f.controller.save(true)
  try {
    assert.equal(f.calls.find(call => call[0] === 'save')[1].expected_story_version, 5)
    assert.equal(f.controller.state.draft.title, '本地保留')
    assert.equal(f.calls.filter(call => call[0] === 'generate').length, 0)
  } finally { await f.controller.dispose() }
})

test('failed generated candidates are recoverable and adopted candidates do not reappear', async () => {
  const f = fixture()
  f.setTasks([{ id: 'failed-run', status: 'failed', state: { story_candidate: { draft }, media_input: { request_id: 'old' } } }])
  await f.controller.load('chat'); assert.equal(f.controller.state.candidateRunId, 'failed-run')
  await f.controller.mutate('candidate')
  f.setTasks([{ id: 'failed-run', status: 'failed', state: { story_candidate: { draft } } },
    { id: 'adopted-run', status: 'completed', state: { story_result: { candidate_run_id: 'failed-run' } } }])
  try { await f.controller.load(); assert.equal(f.controller.state.candidateRunId, '') }
  finally { await f.controller.dispose() }
})

test('a rejected generation immediately discovers its candidate without resubmitting or replacing the draft', async () => {
  const initial = { ...clone(view), review: null, confirmation: null }
  const f = fixture(initial)
  await f.controller.load('chat')
  let submissions = 0, reads = 0
  f.api.generateComicStory = async () => {
    submissions++
    f.setTasks([{ id: 'retained-draft', status: 'failed', state: { story_candidate: { draft } } }])
    throw new CoreApiError('镜头 1 · 运镜：审核依据不一致，候选已保留', 400)
  }
  const requests = f.api.getComicStoryRequests
  f.api.getComicStoryRequests = async () => { reads++; return requests() }
  try {
    assert.equal(await f.controller.mutate('generate'), false)
    assert.equal(submissions, 1); assert.equal(reads, 1)
    assert.equal(f.controller.state.operation, '')
    assert.equal(f.controller.state.unknownRequest, null)
    assert.equal(f.controller.state.candidateRunId, 'retained-draft')
    assert.match(f.controller.state.error, /运镜.*候选已保留/)
    assert.deepEqual(clone(f.controller.state.draft), draft)
    assert.equal(f.controller.canConfirm.value, false)
    assert.equal(f.controller.canExport.value, false)
  } finally { await f.controller.dispose() }
})

test('candidate lookup failure preserves the original generation error and unlocks the button', async () => {
  const f = fixture()
  await f.controller.load('chat')
  f.api.generateComicStory = async () => { throw new CoreApiError('镜头 2 的审核未完成', 400) }
  f.api.getComicStoryRequests = async () => { throw new CoreApiError('候选查询失败', 503) }
  try {
    assert.equal(await f.controller.mutate('generate'), false)
    assert.equal(f.controller.state.error, '镜头 2 的审核未完成')
    assert.equal(f.controller.state.operation, '')
    assert.equal(f.controller.state.unknownRequest, null)
  } finally { await f.controller.dispose() }
})

test('changing chat while a response is pending isolates the next conversation', async () => {
  const f = fixture(), pending = deferred(), originalGet = f.api.getComicStory
  f.api.getComicStory = async owner => owner === 'old' ? pending.promise : originalGet(owner)
  const old = f.controller.load('old'); await flush()
  f.setView({ ...clone(view), story: { ...clone(story), title: '新聊天故事' } }); await f.controller.load('new')
  pending.resolve({ ...clone(view), story: { ...clone(story), title: '旧聊天晚到' } }); await old
  try { assert.equal(f.controller.state.owner, 'new'); assert.equal(f.controller.state.draft.title, '新聊天故事') }
  finally { await f.controller.dispose() }
})

test('export requires current confirmation and shows exact missing shot with repair actions', async () => {
  const f = fixture({ ...clone(view), missing_images: [{ shot_id: 'stable-shot', image_id: 'first', version: 1, reason: '文件缺失', source_project_id: 'project-a' }] })
  await f.controller.load('chat')
  try {
    assert.equal(f.controller.canExport.value, false)
    const html = await renderToString(createSSRApp({ render: () => h(StoryStage, { stage: 'export', workbench: f.controller }) }))
    assert.match(html, /镜头 1 图片不可导出/); assert.match(html, /文件缺失/); assert.match(html, /替换镜头图片/); assert.match(html, /到图片工作台重传/)
    await f.controller.download(); assert.equal(f.calls.filter(call => call[0] === 'export').length, 0)
  } finally { await f.controller.dispose() }
})

test('confirmed export downloads the server ZIP Artifact and never invokes a model', async () => {
  const f = fixture(), oldDocument = globalThis.document, downloads = []
  globalThis.document = { createElement: () => ({ href: '', download: '', click() { downloads.push({ href: this.href, filename: this.download }) } }) }
  await f.controller.load('chat')
  try {
    await f.controller.download(); assert.deepEqual(downloads, [{ href: 'blob:zip', filename: 'story-v1.zip' }])
    assert.equal(f.calls.filter(call => call[0] === 'generate' || call[0] === 'review').length, 0)
    f.controller.state.draft.title = '新稿'; f.controller.changed(); assert.equal(f.controller.canExport.value, false)
  } finally { await f.controller.dispose(); globalThis.document = oldDocument }
})

test('export explains its gate and can review then confirm the same story before download', async () => {
  const f = fixture({ ...clone(view), review: null, confirmation: null })
  await f.controller.load('chat')
  const render = () => renderToString(createSSRApp({ render: () => h(StoryStage, { stage: 'export', workbench: f.controller }) }))
  try {
    assert.match(await render(), /尚未完成校验/)
    assert.match(await render(), /校验当前故事/)
    assert.equal(f.controller.canExport.value, false)
    await f.controller.mutate('review')
    assert.match(await render(), /确认当前故事/)
    assert.equal(f.controller.canExport.value, false)
    await f.controller.mutate('confirm')
    assert.equal(f.controller.canExport.value, true)
    assert.deepEqual(f.calls.filter(call => ['review', 'confirm', 'generate'].includes(call[0])).map(call => call[0]), ['review', 'confirm'])
    assert.doesNotMatch(await render(), /当前故事尚未完成校验/)
  } finally { await f.controller.dispose() }
})

test('story and export both render the frozen image even after another version is adopted', async () => {
  const f = fixture()
  f.api.getComicStoryImages = async () => ({ images: [{ ...image, version: 9, artifact_id: 'first-v9' }] })
  await f.controller.load('chat')
  try {
    for (const stage of ['story', 'export']) {
      await f.controller.showImages([image])
      const html = await renderToString(createSSRApp({ render: () => h(StoryStage, { stage, workbench: f.controller }) }))
      assert.match(html, /src="blob:first-v1"/)
      assert.match(html, stage === 'export' ? /导出镜头 1 的图片/ : /镜头 1 使用的图片/)
    }
  } finally { await f.controller.dispose() }
})

test('paging candidate images keeps every visible story preview and releases hidden images', async () => {
  const f = fixture(); await f.controller.load('chat')
  const pages = Array.from({ length: 24 }, (_, index) => ({ artifact_id: 'candidate-' + index }))
  try {
    for (let page = 0; page < 3; page++) {
      await f.controller.showImages([image, ...pages.slice(page * 8, page * 8 + 8)])
      assert.equal(f.controller.state.previews[image.artifact_id], 'blob:first-v1')
      assert.equal(Object.keys(f.controller.state.previews).length, 9)
      if (page) assert.equal(f.controller.state.previews['candidate-' + ((page - 1) * 8)], undefined)
    }
  } finally { await f.controller.dispose() }
})

test('decode errors are visible and retry rereads the same original without any model call', async () => {
  const f = fixture(); await f.controller.load('chat')
  try {
    f.controller.previewFailed(image)
    assert.equal(f.controller.state.previews[image.artifact_id], undefined)
    assert.match(f.controller.state.previewErrors[image.artifact_id], /无法显示/)
    await f.controller.preview(image, true)
    assert.equal(f.controller.state.previews[image.artifact_id], 'blob:first-v1')
    assert.equal(f.controller.state.previewErrors[image.artifact_id], undefined)
    assert.equal(f.calls.some(call => ['generate', 'review'].includes(call[0])), false)
  } finally { await f.controller.dispose() }
})

test('a failed adopted-image lookup never discards the readable story, history or local edits', async () => {
  const f = fixture()
  f.api.getComicStoryImages = async () => { throw new CoreApiError('已采用图片暂不可读取 · ERR-IMAGE', 400) }
  await f.controller.load('chat')
  try {
    assert.equal(f.controller.state.draft.title, draft.title)
    assert.equal(f.controller.state.versions[0].version, story.version)
    assert.match(f.controller.state.error, /ERR-IMAGE/)
    f.controller.state.draft.summary = '用户仍在修改的剧情'; f.controller.changed()
    await f.controller.load()
    assert.equal(f.controller.state.draft.summary, '用户仍在修改的剧情')
    assert.equal(f.controller.dirty.value, true)
    assert.equal(f.calls.some(call => ['generate', 'review', 'confirm'].includes(call[0])), false)
  } finally { await f.controller.dispose() }
})

test('unavailable adopted versions retain identity but cannot preview, join a shot or submit as new inputs', async () => {
  const missing = { ...image, available: false, lifecycle_state: 'purged', unavailable_reason: '原图片已彻底删除' }
  const f = fixture({ ...clone(view), missing_images: [{ shot_id: shot.shot_id, image_id: image.image_id, image_version: image.version, reason: missing.unavailable_reason }] })
  f.api.getComicStoryImages = async () => ({ images: [missing, clone(second)], unavailable_images: [{ image_id: 'bad-history', version: 99, reason: '历史采用版本不存在' }] })
  await f.controller.load('chat')
  try {
    assert.equal(f.controller.state.images[0].image_id, image.image_id)
    assert.equal(f.calls.some(call => call[0] === 'preview' && call[1] === image.artifact_id), false)
    f.controller.toggleImage(missing); f.controller.addImage(missing)
    assert.deepEqual(f.controller.state.selectedImages, [])
    assert.equal(f.controller.state.draft.shots.length, 1)
    f.controller.toggleImage(second)
    assert.deepEqual(f.controller.state.selectedImages, [storyImageKey(second)])
    const html = await renderToString(createSSRApp({ render: () => h(StoryStage, { stage: 'story', workbench: f.controller }) }))
    assert.match(html, /原图片已彻底删除/)
    assert.match(html, /已采用图片 1 · v99 不可用：历史采用版本不存在/)
    assert.match(html, /disabled[^>]*aria-label="选入 horse.png v1"/)
    assert.equal(f.controller.canConfirm.value, false)
    assert.equal(f.controller.canExport.value, false)
  } finally { await f.controller.dispose() }
})

/* 大图预览必须能回到故事编排：
   ① 预览层挂在 body 上 —— 留在 .stage-scroll 里会被滚动容器裁剪，
      关闭按钮落到工作台页眉下面，空白处也点不到（用户因此回不去）；
   ② Esc 绑在 window 而不是弹层自身 —— 焦点被点到外壳后弹层收不到按键；
   ③ 保留可见的关闭按钮、画布空白处关闭，并在关闭后还原滚动位置。 */
test('the enlarged image preview is a full-screen layer that always offers a way back', () => {
  const source = readFileSync(new URL('../src/components/comic/ComicFlowStage.vue', import.meta.url), 'utf8')
  const template = source.slice(source.indexOf('<template>'))
  assert.match(template, /<Teleport to="body">[\s\S]*class="flow-large-image"[\s\S]*<\/Teleport>/)
  assert.doesNotMatch(source, /@keydown\.esc="largeImage = null"/)
  assert.match(source, /addEventListener\('keydown', onKeydown, true\)/)
  assert.match(source, /removeEventListener\('keydown', onKeydown, true\)/)
  assert.match(template, /class="ui-button flow-large-close"[^>]*@click="closeImage"/)
  assert.match(template, /@empty="closeImage"/)
  assert.match(source, /scroll\.target\.scrollTop = scroll\.top/)
  const viewer = readFileSync(new URL('../src/components/ui/ImageViewport.vue', import.meta.url), 'utf8')
  assert.match(viewer, /defineEmits<\{ error: \[\]; empty: \[\] \}>/)
  assert.match(viewer, /@click\.self="zoom <= 1 && emit\('empty'\)"/)
})
