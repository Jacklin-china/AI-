import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'

const source = readFileSync(new URL('../src/components/layout/ComicWorkspaceShell.vue', import.meta.url), 'utf8')
const { descriptor, errors } = parse(source)
assert.deepEqual(errors, [])
const script = compileScript(descriptor, { id: 'comic-shell-contract', inlineTemplate: true }).content
const module = ts.transpileModule(script, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  .replace(/from (["'])vue\1/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`)
  .replace(/from (["'])lucide-vue-next\1/g, `from ${JSON.stringify(import.meta.resolve('lucide-vue-next'))}`)
const { default: Shell } = await import(`data:text/javascript;base64,${Buffer.from(module).toString('base64')}`)
const pages = ['对话', '导演', '分镜', '资产', 'Prompt', '历史']
const Icon = () => h('span', { 'aria-hidden': true })
async function render(page, navigationOpen = true) {
  return renderToString(createSSRApp({ render: () => h(Shell, {
    navigation: pages.map(label => ({ id: label, label, icon: Icon })), activePage: page,
    navigationOpen, creatingConversation: false,
  }, {
    recent: () => h('button', { 'aria-current': 'true' }, '当前创作对话'),
    toolbar: () => h('span', '当前作品'), heading: () => h('h2', page),
    default: () => h('article', `当前页面：${page}`),
    composer: () => h('textarea', { value: '尚未发送的补充', 'aria-label': '创作输入' }),
  }) }))
}

test('all six pages keep recent conversations and input in the same shared shell', async () => {
  const html = await Promise.all(pages.map(page => render(page)))
  const sidebars = html.map(text => text.slice(text.indexOf('<aside'), text.indexOf('</aside>') + 8).replace(/ aria-current="page"/g, ''))
  assert.ok(sidebars.every(sidebar => sidebar === sidebars[0]))
  for (let i = 0; i < pages.length; i++) {
    assert.match(html[i], /最近对话|当前创作对话/)
    assert.match(html[i], /尚未发送的补充/)
    assert.match(html[i], new RegExp(`当前页面：${pages[i]}`))
    assert.equal((html[i].match(/<textarea/g) ?? []).length, 1)
    assert.doesNotMatch(html[i], /<header|<footer/)
  }
})

test('collapsing the second sidebar keeps its conversations and composer mounted', async () => {
  const html = await render('导演', false)
  assert.match(html, /<aside[^>]*display:none/)
  assert.match(html, /aria-label="展开导航"/)
  assert.match(html, /当前创作对话/)
  assert.match(html, /尚未发送的补充/)
})
