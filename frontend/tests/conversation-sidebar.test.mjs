import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/components/chat/conversationPresentation.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const { conversationTaskTitle, ownsConversationRun } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

test('first task gets a short title without changing the original request', () => {
  const cases = [
    ['我想制作一张中式修仙少女站在树梢看村庄', '修仙少女树梢场景'],
    ['山海经穷奇站在悬崖看村庄', '穷奇悬崖场景'],
    ['请帮我制作一个机器人坐在咖啡店等待顾客', '机器人咖啡店场景'],
  ]
  for (const [request, expected] of cases) assert.equal(conversationTaskTitle(request), expected)
  assert.ok(Array.from(conversationTaskTitle('请制作一个非常长的完全不同的童话主题创作任务')).length <= 14)
})

test('only exact Conversation ID bindings restore runs, even for shared projects', () => {
  assert.equal(ownsConversationRun({ state: { conversation_id: 'old', project_id: 'shared' } }, 'new'), false)
  assert.equal(ownsConversationRun({ state: { conversation_id: 'new', project_id: 'shared' } }, 'new'), true)
  assert.equal(ownsConversationRun({ state: {} }, ''), false)
})

test('homepage and Comic reuse the same shell, history list and conversation-keyed composer', () => {
  const home = readFileSync(new URL('../src/views/HomeView.vue', import.meta.url), 'utf8')
  const comic = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  for (const view of [home, comic]) {
    assert.match(view, /WorkspaceShell/)
    assert.match(view, /<ConversationHistory list-only/)
    assert.match(view, /conversationTaskTitle/)
  }
  assert.match(comic, /const epoch = \+\+conversationEpoch\s+resetConversationView\(\)/)
  assert.match(comic, /const conversation = owner/)
  assert.match(comic, /owner !== activeConversationId.value/)
  assert.doesNotMatch(comic, /kantoku-comic-conversation:|getCurrentDirector\(/)
})

test('switching conversation during submission keeps the original owner and ignores its late response', async () => {
  const comic = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const start = comic.indexOf('async function execute(')
  const end = comic.indexOf('\nfunction sendInput(', start)
  const body = comic.slice(start, end).replace(/\bconversationEpoch\b/g, 'environment.epoch').replace(/\bdisposed\b/g, 'environment.disposed')
  const output = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const ref = value => ({ value })
  let releaseProject
  const created = new Promise(resolve => { releaseProject = resolve })
  const submissions = []
  const environment = {
    epoch: 1, disposed: false, busy: ref(false), hasRunning: ref(false), project: ref(null),
    composer: ref(null), draftKey: ref('old-draft'), activeConversationId: ref('old'),
    mode: ref('fast'), manualDirectorApproval: ref(true), pendingText: ref(''), previousRunIds: ref([]), executions: ref([]),
    error: ref(''), restoredSpec: ref(null), selectedRun: ref(''), drafts: ref({}),
    invalidate() {}, createComicProject: () => created,
    getComicProject() { throw new Error('unexpected project inheritance') },
    async createDirectorExecution(id, options) { submissions.push({ id, options }); return { run_id: 'old-run' } },
    refresh() { throw new Error('late response must not refresh the new view') },
    loadConversations() { throw new Error('late response must not select a conversation') },
    failureText: String, selectDirectorExecution() { return null },
  }
  const names = Object.keys(environment).filter(name => name !== 'epoch' && name !== 'disposed')
  const execute = new Function('environment', `const {${names.join(',')}} = environment; ${output}; return execute`)(environment)
  const pending = execute('少年雨夜思念故乡')
  environment.epoch++
  environment.activeConversationId.value = 'new'
  environment.busy.value = false
  environment.pendingText.value = ''
  releaseProject({ project: { project_id: 'old-project', current_version: 1 } })
  await pending
  assert.equal(submissions.length, 1)
  assert.equal(submissions[0].options.conversation_id, 'old')
  assert.equal(submissions[0].options.task, '少年雨夜思念故乡')
  assert.equal(environment.project.value, null)
  assert.equal(environment.selectedRun.value, '')
  assert.equal(environment.error.value, '')
})

test('automatic creation late response cannot bind its project or image to the new conversation', async () => {
  const comic = readFileSync(new URL('../src/components/DirectorWorkspace.vue', import.meta.url), 'utf8')
  const start = comic.indexOf('async function execute(')
  const body = comic.slice(start, comic.indexOf('\nfunction sendInput(', start))
    .replace(/\bconversationEpoch\b/g, 'environment.epoch').replace(/\bdisposed\b/g, 'environment.disposed')
  const output = ts.transpileModule(body, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const ref = value => ({ value })
  let release
  const response = new Promise(resolve => { release = resolve })
  const submissions = []
  const environment = {
    epoch: 1, disposed: false, busy: ref(false), hasRunning: ref(false), project: ref(null),
    composer: ref(null), draftKey: ref('draft'), activeConversationId: ref('old'),
    mode: ref('fast'), manualDirectorApproval: ref(false), pendingText: ref(''),
    previousRunIds: ref([]), executions: ref([]), error: ref(''), restoredSpec: ref(null),
    productionRun: ref(null), runs: ref({}), invalidate() {},
    createRun(domain, payload) { submissions.push({ domain, payload }); return response },
    getComicProject() { throw new Error('must not load a late project into the new view') },
    refresh() { throw new Error('must not refresh another conversation') },
    failureText: String,
  }
  const names = Object.keys(environment).filter(name => name !== 'epoch' && name !== 'disposed')
  const execute = new Function('environment', `const {${names.join(',')}}=environment; let pendingCreation=null,productionAdvanced=false; ${output}; return execute`)(environment)
  const pending = execute('少女竹林')
  environment.epoch++
  environment.activeConversationId.value = 'new'
  environment.busy.value = false
  environment.pendingText.value = ''
  release({ id: 'old-production', state: { quick_creation: { project_id: 'old-project' } } })
  await pending
  assert.equal(submissions.length, 1)
  assert.equal(submissions[0].payload.conversation_id, 'old')
  assert.equal(environment.productionRun.value, null)
  assert.equal(environment.project.value, null)
  assert.equal(environment.error.value, '')
})
