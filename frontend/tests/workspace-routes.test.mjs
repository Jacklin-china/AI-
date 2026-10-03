import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

// Execute the real router with a browser-history harness, not a duplicate route implementation.
const source = readFileSync(new URL('../src/router.ts', import.meta.url), 'utf8')
let popstate
const paths = []
globalThis.window = {
  location: { pathname: '/', search: '' },
  history: {
    pushState(_state, _title, path) { paths.push(path); window.location.pathname = path },
    replaceState(_state, _title, path) { window.location.pathname = path },
  },
  scrollTo() {}, addEventListener(_event, listener) { popstate = listener },
}
const compiled = ts.transpileModule(source.replace("import { ref } from 'vue'", 'const ref = value => ({ value })'), { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const { parse, href, navigate, route, directorStagePaths } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

test('all director nodes have independent refreshable URLs bound to the same Run', () => {
  for (const [directorStage, slug] of Object.entries(directorStagePaths)) {
    const target = { name: 'workspace_run', domain: 'comic', runId: 'run 1', workspacePage: 'director', directorStage }
    assert.equal(href(target), `/workspace/comic/run/run%201/director/${slug}`)
    assert.deepEqual(parse(href(target)), target)
  }
})

test('Conversation selection survives refresh on every node URL', () => {
  const target = { name: 'workspace_run', domain: 'comic', runId: 'old-run', workspacePage: 'director', directorStage: 'cinematography', conversationId: 'new chat' }
  assert.deepEqual(parse(href(target)), target)
  assert.deepEqual(parse('/?conversation=home-chat'), { name: 'home', conversationId: 'home-chat' })
})
test('explicit professional entry keeps mode and conversation on refresh', () => {
  const target = { name: 'workspace', domain: 'comic', workspacePage: 'director', directorStage: 'creative_understanding', conversationId: 'home-chat', creationMode: 'professional' }
  assert.deepEqual(parse(href(target)), target)
  assert.equal(parse('/workspace/comic?mode=unsafe').creationMode, undefined)
})
test('project pages and legacy workspace URLs remain compatible', () => {
  for (const workspacePage of ['conversation', 'assets', 'storyboard', 'prompt', 'history']) {
    const target = { name: 'workspace', domain: 'comic', workspacePage }
    assert.deepEqual(parse(href(target)), target)
  }
  assert.deepEqual(parse('/workspace/comic/run/old-run'), { name: 'workspace_run', domain: 'comic', runId: 'old-run' })
  assert.deepEqual(parse('/workspace/comic'), { name: 'workspace', domain: 'comic' })
  assert.equal(parse('/workspace/comic/director/nonexistent').directorStage, 'director_assemble')
  assert.equal(href({ name: 'workspace', domain: 'comic', workspacePage: 'not-a-page' }), '/workspace/comic')
})
test('history navigation restores node location without changing conversation mount identity', () => {
  const target = { name: 'workspace_run', domain: 'comic', runId: 'original-run', workspacePage: 'director', directorStage: 'cinematography' }
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
