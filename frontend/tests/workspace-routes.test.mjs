import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

// Execute the real router with a browser-history harness, not a duplicate route implementation.
const source = readFileSync(new URL('../src/router.ts', import.meta.url), 'utf8')
let popstate
const paths = []
const navigationStorage = new Map()
globalThis.window = {
  location: { pathname: '/', search: '' },
  history: {
    pushState(_state, _title, path) { paths.push(path); window.location.pathname = path },
    replaceState(_state, _title, path) { window.location.pathname = path },
  },
  scrollTo() {}, addEventListener(_event, listener) { popstate = listener },
  sessionStorage: {
    getItem(key) { return navigationStorage.get(key) ?? null },
    setItem(key, value) { navigationStorage.set(key, value) },
  },
}
const compiled = ts.transpileModule(source.replace("import { ref } from 'vue'", 'const ref = value => ({ value })'), { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const routerModule = `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`
const { parse, href, navigate, route, directorStagePaths, workspaceEntry } = await import(routerModule)

test('all director nodes have independent refreshable URLs bound to the same Run', () => {
  for (const [directorStage, slug] of Object.entries(directorStagePaths)) {
    const target = { name: 'workspace_run', domain: 'comic', runId: 'run 1', workspacePage: 'plan', directorStage }
    assert.equal(href(target), `/workspace/comic/run/run%201/plan/${slug}`)
    assert.deepEqual(parse(href(target)), target)
  }
})

test('Conversation selection survives refresh on every node URL', () => {
  const target = { name: 'workspace_run', domain: 'comic', runId: 'old-run', workspacePage: 'plan', directorStage: 'cinematography', conversationId: 'new chat' }
  assert.deepEqual(parse(href(target)), target)
  assert.deepEqual(parse('/?conversation=home-chat'), { name: 'home', conversationId: 'home-chat' })
})
test('imported and director works retain distinct scopes across nodes and refresh', () => {
  for (const workspacePage of ['conversation', 'images', 'prompt', 'story', 'export']) {
    const imported = { name: 'workspace', domain: 'comic', workspacePage,
      conversationId: 'chat', workId: 'same-project:brief:1' }
    const director = { ...imported, workId: 'same-project:brief:2', directorRunId: 'cowboy' }
    assert.deepEqual(parse(href(imported)), imported)
    assert.deepEqual(parse(href(director)), director)
  }
})

test('each conversation turn opens its own plan and survives refresh and back navigation', () => {
  const first = { name: 'workspace', domain: 'comic', workspacePage: 'plan',
    directorStage: 'director_assemble', conversationId: 'same-chat', directorRunId: 'turn 1' }
  const second = { ...first, directorRunId: 'turn 2' }
  assert.notEqual(href(first), href(second))
  assert.deepEqual(parse(href(first)), first)
  assert.deepEqual(parse(href(second)), second)
  navigate(first)
  navigate(second)
  window.location.pathname = href(first)
  popstate()
  assert.equal(route.value.directorRunId, 'turn 1')
  assert.equal(route.value.conversationId, 'same-chat')
})
test('explicit professional entry keeps mode and conversation on refresh', () => {
  const target = { name: 'workspace', domain: 'comic', workspacePage: 'plan', directorStage: 'creative_understanding', conversationId: 'home-chat', creationMode: 'professional' }
  assert.deepEqual(parse(href(target)), target)
  assert.equal(parse('/workspace/comic?mode=unsafe').creationMode, undefined)
})
test('project pages and legacy workspace URLs remain compatible', () => {
  for (const workspacePage of ['conversation', 'assets', 'story', 'prompt', 'history']) {
    const target = { name: 'workspace', domain: 'comic', workspacePage }
    assert.deepEqual(parse(href(target)), target)
  }
  assert.deepEqual(parse('/workspace/comic/run/old-run'), { name: 'workspace_run', domain: 'comic', runId: 'old-run' })
  assert.deepEqual(parse('/workspace/comic'), { name: 'workspace', domain: 'comic' })
  assert.equal(parse('/workspace/comic/director/nonexistent').directorStage, 'director_assemble')
  assert.equal(href({ name: 'workspace', domain: 'comic', workspacePage: 'not-a-page' }), '/workspace/comic')
})

test('work task routes restore exact ownership while story routes only require the work', () => {
  const task = { name: 'workspace', domain: 'comic', workspacePage: 'images', workId: 'work-a', taskId: 'task-a' }
  assert.deepEqual(parse(href(task)), task)
  const second = { ...task, taskId: 'task-b' }
  assert.notEqual(href(task), href(second))
  assert.deepEqual(parse(href({ name: 'workspace', domain: 'comic', workspacePage: 'story', workId: 'work-a' })),
    { name: 'workspace', domain: 'comic', workspacePage: 'story', workId: 'work-a' })
})
test('history navigation restores node location without changing conversation mount identity', () => {
  const target = { name: 'workspace_run', domain: 'comic', runId: 'original-run', workspacePage: 'plan', directorStage: 'cinematography' }
  navigate(target)
  const previous = window.location.pathname
  navigate({ ...target, directorStage: 'visual_direction' })
  const count = paths.length
  navigate({ ...target, directorStage: 'visual_direction' })
  assert.equal(paths.length, count)
  window.location.pathname = previous
  popstate()
  assert.deepEqual(route.value, target)
  navigate({ ...target, directorStage: 'director_assemble' }, true)
  assert.equal(paths.length, count)
  assert.equal(route.value.directorStage, 'director_assemble')
})

test('the actual global workspace action resumes the exact work and page through every sidebar', () => {
  const app = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
  const code = ts.transpileModule(app.slice(app.indexOf('function go('), app.indexOf('\nfunction shortcut(')),
    { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  const go = new Function('parse', 'navigate', 'workspaceEntry', `${code}; return go`)(parse, navigate, workspaceEntry)
  for (const workspacePage of ['conversation', 'images', 'story', 'plan']) {
    const target = { name: 'workspace', domain: 'comic', workId: 'selected-work',
      taskId: 'selected-task', directorRunId: 'selected-plan', workspacePage }
    navigate(target)
    for (const outside of ['/assets', '/tasks', '/settings', '/']) {
      go(outside)
      assert.notEqual(route.value.name, 'workspace')
      go('/workspace/comic')
      assert.deepEqual(route.value, target)
      go('/workspace/comic')
      assert.deepEqual(route.value, target)
    }
  }
})

test('a reload outside the workspace restores saved scope; invalid storage never becomes a selection', async () => {
  const saved = { name: 'workspace', domain: 'comic', workId: 'saved-work', taskId: 'saved-task', workspacePage: 'images' }
  navigate(saved)
  navigate({ name: 'assets' })
  const reloaded = await import(`${routerModule}#reload`)
  assert.deepEqual(reloaded.workspaceEntry(), saved)
  assert.deepEqual(reloaded.workspaceEntry('commerce'), { name: 'workspace', domain: 'commerce' })
  for (const invalid of ['/settings?work=wrong', '/workspace/commerce?work=other', '/workspace/comic']) {
    navigationStorage.set('kantoku-workspace-route:comic', invalid)
    assert.deepEqual(reloaded.workspaceEntry(), { name: 'workspace', domain: 'comic' })
  }
  const storage = window.sessionStorage
  window.sessionStorage = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('blocked') } }
  try {
    navigate(saved)
    navigate({ name: 'home' })
    assert.deepEqual(workspaceEntry(), saved)
  } finally { window.sessionStorage = storage }
})
