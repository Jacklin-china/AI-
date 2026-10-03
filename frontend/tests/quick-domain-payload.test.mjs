import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

test('home stream sends a real one-message domain and mode, including explicit null', async () => {
  const core = readFileSync(new URL('../src/services/core.ts', import.meta.url), 'utf8')
  const start = core.indexOf('export async function streamConversationMessage(')
  const end = core.indexOf('\nexport ', start + 7)
  const source = core.slice(start, end < 0 ? undefined : end)
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
  }).outputText.replace('export async function', 'async function').replace(/export \{\};?/g, '')
  const requests = []
  const fetch = async (_url, request) => {
    requests.push(JSON.parse(request.body))
    return { ok: true, body: { getReader: () => ({
      read: async () => ({ done: true, value: undefined }),
    }) } }
  }
  const invoke = new Function('ensureToken', 'fetch', 'api', 'token', 'apiFailure',
    `${compiled}; return streamConversationMessage`)(async () => {}, fetch, x => x, 'test', () => {})
  for (const domain of ['comic', 'commerce', 'studio', null]) {
    await invoke('conversation-owner', '当前任务', domain, { onDelta() {} }, undefined,
      false, 'generation-owner', { selected_domain: domain, execution_mode: domain ? 'fast' : 'normal' })
  }
  assert.deepEqual(requests.map(({ selected_domain, execution_mode }) => ({ selected_domain, execution_mode })), [
    { selected_domain: 'comic', execution_mode: 'fast' },
    { selected_domain: 'commerce', execution_mode: 'fast' },
    { selected_domain: 'studio', execution_mode: 'fast' },
    { selected_domain: null, execution_mode: 'normal' },
  ])
  assert.ok(requests.every(item => item.generation_request_id === 'generation-owner'))
  const home = readFileSync(new URL('../src/views/HomeView.vue', import.meta.url), 'utf8')
  assert.match(home, /selected_domain: task.domainHint, execution_mode: task.domainHint \? 'fast' : 'normal'/)
})

test('actual quick director nodes have human-readable activity labels', () => {
  const presenter = readFileSync(new URL('../src/domains/presenters.ts', import.meta.url), 'utf8')
  const messages = readFileSync(new URL('../src/components/chat/ChatMessageList.vue', import.meta.url), 'utf8')
  for (const node of ['director', 'director_gate', 'storyboard', 'prompt']) {
    assert.match(presenter, new RegExp(`${node}: '[^']+'`))
    assert.match(messages, new RegExp(`${node}: '正在[^']+'`))
  }
})
