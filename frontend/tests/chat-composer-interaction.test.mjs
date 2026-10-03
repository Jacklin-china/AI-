import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createRenderer, h, nextTick } from 'vue'

const { descriptor } = parse(readFileSync(new URL('../src/components/chat/MessageComposer.vue', import.meta.url), 'utf8'))
const script = compileScript(descriptor, { id: 'composer-events', inlineTemplate: true }).content
const code = ts.transpileModule(script, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  .replace(/from (["'])vue\1/g, `from ${JSON.stringify(import.meta.resolve('vue'))}`)
  .replace(/from (["'])lucide-vue-next\1/g, `from ${JSON.stringify(import.meta.resolve('lucide-vue-next'))}`)
const { default: Composer } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)

// Run the actual Vue handlers with an in-memory host; no layout/CSS/browser claims.
function element(tag) {
  return { tag, props: {}, children: [], parent: null, style: {}, value: '', scrollHeight: 40,
    listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn }, focus() {} }
}
const renderer = createRenderer({
  createElement: element, createText: text => ({ ...element('#text'), text }),
  createComment: text => ({ ...element('#comment'), text }),
  setText: (node, text) => { node.text = text },
  setElementText: (node, text) => { node.text = text; node.children = [] },
  patchProp: (node, key, _old, value) => { node.props[key] = value },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
  insert(node, parent, anchor) {
    if (node.parent) this.remove(node)
    node.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, node)
  },
  remove(node) { const list = node.parent?.children; if (list) list.splice(list.indexOf(node), 1) },
})
function find(node, predicate) {
  if (predicate(node)) return node
  for (const child of node.children) { const found = find(child, predicate); if (found) return found }
  return null
}

test('actual plus handler opens/closes the menu and each domain emits its real ID', async () => {
  for (const domain of ['comic', 'commerce', 'studio']) {
    const host = element('root')
    const selected = []
    const app = renderer.createApp({ render: () => h(Composer, {
      fastDomains: true, fastDomainBusy: false, onSelectFastDomain: value => selected.push(value),
    }) })
    app.mount(host)
    const plus = () => find(host, node => node.props['aria-label'] === '选择创作域快捷模式')
    assert.equal(plus().props.disabled, false)
    plus().props.onClick(); await nextTick()
    assert.equal(plus().props['aria-expanded'], true)
    const menu = find(host, node => node.props.role === 'menu')
    assert.ok(menu)
    menu.children.filter(node => node.props.role === 'menuitem')[['comic', 'commerce', 'studio'].indexOf(domain)].props.onClick()
    await nextTick()
    assert.deepEqual(selected, [domain])
    assert.equal(plus().props['aria-expanded'], false)
    plus().props.onClick(); await nextTick()
    plus().props.onClick(); await nextTick()
    assert.equal(find(host, node => node.props.role === 'menu'), null)
    app.unmount()
  }
})
