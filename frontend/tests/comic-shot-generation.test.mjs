import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'

const compile = code => ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const moduleUrl = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const { shotGeneration } = await import(moduleUrl(compile(readFileSync(new URL('../src/domains/comic/shotGeneration.ts', import.meta.url), 'utf8'))))
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

test('actual Shot submission reuses network request identity and captures edited Prompt only on explicit retry', async () => {
  const workspace = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const code = compile(workspace.slice(workspace.indexOf('async function generateImage('), workspace.indexOf('async function recoverImage(')))
  const ref = value => ({ value })
  const submissions = []
  let promptReads = 0
  const failed = run('run1', 'shot1', 'failed')
  const state = {
    productionEligible: ref(true), manualDirectorApproval: ref(true), confirmed: ref(true),
    project: ref({ project: { project_id: 'project', current_version: 3 } }), spec: ref({ version: 1 }),
    submittingImage: ref(false), compiling: ref(false), busy: ref(false), hasRunning: ref(false),
    section: ref('storyboard'), shots: ref([{ shot_id: 'shot1', version: 1 }]), selectedShot: ref('shot1'),
    error: ref(''), activeConversationId: ref('conversation'), productionRun: ref(null), runs: ref({}), productionEvents: ref([]),
    productionRequests: new Map(), productionPromptVersions: new Map(),
    generationForShot() { return failed },
    async getComicPromptVersions() { promptReads++; return promptReads === 1 ? [] : [{ version: 2 }] },
    async createRun(domain, payload) { submissions.push(payload); return { id: 'result', state: {} } },
    async refresh() {}, async loadPage() {}, openSection() {}, failureText: String,
  }
  const generate = new Function('state', `const {${Object.keys(state).join(',')}}=state;
    const conversationEpoch=1,disposed=false; let productionAdvanced=false; ${code}; return generateImage`)(state)
  await generate()
  await generate() // lost HTTP response, backend compilation may already have created Prompt v1
  assert.equal(submissions.length, 2)
  assert.equal(submissions[0].request_id, submissions[1].request_id)
  assert.equal(submissions[1].prompt_version, undefined)
  assert.equal(promptReads, 1)
  await generate(true)
  assert.notEqual(submissions[2].request_id, submissions[0].request_id)
  assert.equal(submissions[2].prompt_version, 2)
  assert.equal(submissions[2].shot_id, 'shot1')
  failed.image_execution.can_regenerate = false // unknown bill cannot create a new request
  await generate(true)
  assert.equal(submissions.length, 3)
})
