import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createRenderer, h, nextTick, ref, toRaw } from 'vue'

// Mount real cards, renderers, asset view and shells. Only the HTTP boundary and
// browser host are substituted; no database, Provider or external network is used.
const moduleUrl = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const apiUrl = moduleUrl(`
  let artifacts = []; const media = new Map(); export const calls = [];
  const writes = new Map(); export const lifecycleWrites = [];
  export function setArtifacts(value) { artifacts = value; writes.clear(); lifecycleWrites.length = 0 }
  export function setLifecycleResponse(id, response) { writes.set(id, response) }
  export async function getArtifacts(_runId, lifecycle) { return artifacts.filter(item => !lifecycle || (item.lifecycle_state ?? 'active') === lifecycle) }
  export async function changeArtifactLifecycle(item, action) {
    lifecycleWrites.push({ id: item.id, action, version: item.lifecycle_version ?? 1 });
    const response = writes.get(item.id);
    if (response instanceof Error) throw response;
    const updated = typeof response === 'function' ? await response() : response ?? { ...item, lifecycle_state: action === 'trash' ? 'trashed' : action === 'purge' ? 'purged' : 'active', lifecycle_version: (item.lifecycle_version ?? 1) + 1 };
    artifacts = artifacts.map(value => value.id === updated.id ? updated : value);
    return updated;
  }
  export async function getRuns() { return [] }
  export function setMedia(id, response) { media.set(id, response) }
  export function resetMedia() { media.clear(); calls.length = 0 }
  async function read(id) {
    calls.push(id);
    const value = media.get(id);
    if (value instanceof Error) throw value;
    if (typeof value === 'function') return value();
    return value == null ? null : URL.createObjectURL(new Blob([value], { type: 'image/png' }));
  }
  export async function getArtifactContentUrl(id) { return read('artifact:' + id) }
  export async function getTaskImageUrl(id) { return read('task:' + id) }
`)
const { setArtifacts, setMedia, resetMedia, calls, setLifecycleResponse, lifecycleWrites } = await import(apiUrl)
const modules = new Map()
function loadModule(url) {
  if (url.pathname.endsWith('/services/core.ts')) return apiUrl
  if (modules.has(url.href)) return modules.get(url.href)
  const source = readFileSync(url, 'utf8')
  const script = url.pathname.endsWith('.vue')
    ? compileScript(parse(source).descriptor, { id: url.href, inlineTemplate: true }).content : source
  let code = ts.transpileModule(script, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
  code = code.replace(/from (["'])([^"']+)\1/g, (_match, _quote, path) => {
    const target = path.startsWith('.') ? loadModule(new URL(/\.(vue|ts)$/.test(path) ? path : path + '.ts', url)) : import.meta.resolve(path)
    return `from ${JSON.stringify(target)}`
  })
  const result = moduleUrl(code)
  modules.set(url.href, result)
  return result
}
const { default: AssetsLibrary } = await import(loadModule(new URL('../src/views/AssetsLibrary.vue', import.meta.url)))
const { default: ArtifactCard } = await import(loadModule(new URL('../src/components/artifacts/ArtifactCard.vue', import.meta.url)))

let focused = null
const makeNode = tag => ({ tag, props: {}, children: [], parent: null, style: {}, selectedIndex: -1,
  get open() { return !!this.props.open },
  showModal() { this.props.open = true; this.children.flatMap(all).find(node => node.props.autofocus !== undefined)?.focus() },
  close() { this.props.open = false; this.props.onClose?.() },
  focus() { focused = this },
  get options() { return this.children.filter(node => node.tag === 'option') },
  get value() { return this.tag === 'select' ? this.options[this.selectedIndex]?.value : this.props.value },
  addEventListener() {}, removeEventListener() {} })
const all = node => [node, ...node.children.flatMap(all)]
const remove = node => {
  if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
}
const renderer = createRenderer({
  createElement: makeNode,
  createText: text => ({ ...makeNode('#text'), text }),
  createComment: text => ({ ...makeNode('#comment'), text }),
  setText(node, text) { node.text = text },
  setElementText(node, text) { node.text = text; node.children = [] },
  patchProp(node, key, _previous, value) { node.props[key] = value },
  insert(node, parent, anchor) {
    remove(node)
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, node)
    node.parent = parent
  },
  remove,
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
})
globalThis.window = { matchMedia: () => ({ matches: false }), setTimeout,
  location: { href: 'http://offline.local/assets', search: '' },
  history: { state: null, replaceState(_state, _title, path) {
    const url = new URL(path, window.location.href)
    window.location.href = url.href; window.location.search = url.search
  } },
}
globalThis.ResizeObserver = class { observe() {} disconnect() {} }
const urls = new Map(); const revoked = []
const nativeCreateUrl = URL.createObjectURL; const nativeRevokeUrl = URL.revokeObjectURL
URL.createObjectURL = blob => { const url = nativeCreateUrl(blob); urls.set(url, blob); return url }
URL.revokeObjectURL = url => { revoked.push(url); urls.delete(url); nativeRevokeUrl(url) }
let clipboardText = ''; let clipboardFailure = false; const downloads = []
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { async writeText(value) { if (clipboardFailure) throw new Error('denied'); clipboardText = value } } } })
globalThis.document = { createElement: () => ({ click() { downloads.push({ url: this.href, filename: this.download, blob: urls.get(this.href) }) } }) }

function artifact(id, type, domain = 'comic', origin = 'mock', daysAgo = 0) {
  return { id, type, run_id: 'offline-run', node_id: 'offline-node', status: 'ready', version: 1, location: null, source: `${domain}.offline-fixture`,
    metadata: { origin }, created_at: new Date(Date.now() - daysAgo * 86_400_000).toISOString() }
}
function trashed(id, type = 'prompt') {
  return { ...artifact(id, type), lifecycle_state: 'trashed', lifecycle_version: 2 }
}
// 资产库只保留图片与提示词；其余类型仍然存在，但不得进入列表、计数或全选范围。
const mixed = [
  artifact('image', 'image'), artifact('video', 'video'),
  artifact('document', 'document'), artifact('report', 'report', 'commerce'),
  artifact('prompt', 'prompt'), artifact('json', 'json'), artifact('listing', 'listing'),
]
async function mount(artifacts = mixed) {
  setArtifacts(artifacts)
  const root = makeNode('root')
  const app = renderer.createApp(AssetsLibrary)
  app.mount(root)
  await new Promise(resolve => setImmediate(resolve))
  await nextTick()
  const nodes = () => all(root)
  return {
    nodes,
    ids: () => nodes().filter(node => node.tag === 'article' && node.props['data-artifact-id']).map(node => node.props['data-artifact-id']),
    button: name => nodes().find(node => node.tag === 'button' && node.props['aria-label'] === name),
    select: name => nodes().find(node => node.tag === 'select' && node.props['aria-label'] === name),
    dialog: name => nodes().find(node => node.tag === 'dialog' && node.props['aria-label'] === name),
    async click(name) { this.button(name).props.onClick(); await nextTick() },
    async choose(name, value) { this.select(name).props['onUpdate:modelValue'](value); await nextTick() },
    unmount: () => app.unmount(),
  }
}

test('asset category buttons filter through the real shared shells and keep only images and prompts', async () => {
  const view = await mount()
  try {
    await view.click('图片')
    assert.deepEqual(view.ids(), ['image'])
    assert.equal(view.button('图片').props['aria-current'], 'page')
    assert.equal(view.button('全部产物').props['aria-current'], undefined)
    assert.equal(view.select('按类型筛选').value, 'image')
    await view.click('提示词')
    assert.deepEqual(view.ids(), ['prompt'])
    assert.equal(view.button('提示词').props['aria-current'], 'page')
    assert.equal(view.select('按类型筛选').value, 'prompt')
    await view.click('全部产物')
    assert.deepEqual(view.ids(), ['image', 'prompt'])
    assert.equal(view.button('全部产物').props['aria-current'], 'page')
    assert.equal(view.select('按类型筛选').value, 'all')
  } finally { view.unmount() }
})

test('document, report, listing, json and video artifacts never reach the asset library', async () => {
  const view = await mount()
  try {
    assert.deepEqual(view.ids(), ['image', 'prompt'])
    const text = view.nodes().map(node => node.text ?? '').join(' ')
    assert.ok(text.includes('2 / 2'))
    assert.ok(text.includes('全选仅包括当前显示的 2 项'))
    await clickText(view, '全选当前显示')
    assert.ok(view.nodes().some(node => node.text === '已选 2 项'))
  } finally { view.unmount() }
})

test('single-type dropdown selections keep the matching category highlighted', async () => {
  const view = await mount()
  try {
    for (const [type, category] of [['image', '图片'], ['prompt', '提示词']]) {
      await view.choose('按类型筛选', type)
      assert.deepEqual(view.ids(), [type])
      assert.equal(view.button(category).props['aria-current'], 'page')
    }
  } finally { view.unmount() }
})

test('category selection retains and combines domain, origin and time filters', async () => {
  const view = await mount([
    artifact('kept-image', 'image'), artifact('kept-prompt', 'prompt'),
    artifact('other-domain', 'image', 'commerce'),
    artifact('other-origin', 'image', 'comic', 'blocked'),
    artifact('old-image', 'image', 'comic', 'mock', 40),
  ])
  try {
    await view.choose('按领域筛选', 'comic')
    await view.choose('按来源状态筛选', 'mock')
    await view.choose('按时间筛选', '7')
    await view.click('图片')
    assert.deepEqual(view.ids(), ['kept-image'])
    await view.click('提示词')
    assert.deepEqual(view.ids(), ['kept-prompt'])
    await view.click('全部产物')
    assert.deepEqual(view.ids(), ['kept-image', 'kept-prompt'])
    assert.equal(view.select('按领域筛选').value, 'comic')
    assert.equal(view.select('按来源状态筛选').value, 'mock')
    assert.equal(view.select('按时间筛选').value, '7')
  } finally { view.unmount() }
})

test('an empty category clears old cards while retaining its selected state', async () => {
  const view = await mount([artifact('image', 'image')])
  try {
    await view.click('提示词')
    assert.deepEqual(view.ids(), [])
    assert.equal(view.button('提示词').props['aria-current'], 'page')
    assert.ok(view.nodes().some(node => node.text === '暂无产物'))
    await view.click('图片')
    assert.deepEqual(view.ids(), ['image'])
  } finally { view.unmount() }
})

test('category switches reset pagination and still allow loading more results', async () => {
  const view = await mount([
    ...Array.from({ length: 55 }, (_, index) => artifact(`image-${index}`, 'image')),
    ...Array.from({ length: 55 }, (_, index) => artifact(`prompt-${index}`, 'prompt')),
  ])
  try {
    assert.equal(view.ids().length, 48)
    view.nodes().find(node => node.text === '加载更多').props.onClick()
    await nextTick()
    assert.equal(view.ids().length, 96)
    await view.click('图片')
    assert.equal(view.ids().length, 48)
    view.nodes().find(node => node.text === '加载更多').props.onClick()
    await nextTick()
    assert.equal(view.ids().length, 55)
    await view.click('提示词')
    assert.equal(view.ids().length, 48)
    view.nodes().find(node => node.text === '加载更多').props.onClick()
    await nextTick()
    assert.equal(view.ids().length, 55)
  } finally { view.unmount() }
})

const content = node => [node.text ?? '', ...node.children.map(content)].join(' ')
const settle = async () => { await new Promise(resolve => setImmediate(resolve)); await nextTick() }
async function clickText(view, name) {
  const button = view.nodes().find(node => node.tag === 'button' && content(node).trim() === name)
  assert.ok(button, name)
  if (button.props.disabled) return
  await button.props.onClick(); await settle()
}
// 两个确认弹窗都常驻 DOM：取消/确认必须定位到目标弹窗，不能按文案命中第一个。
async function clickDialog(view, label, name) {
  const dialog = view.dialog(label)
  assert.ok(dialog, label)
  const button = all(dialog).find(node => node.tag === 'button' && content(node).trim() === name)
  assert.ok(button, `${label} / ${name}`)
  if (button.props.disabled) return
  await button.props.onClick(); await settle()
}

test('asset delete cancellation sends no request; single delete survives refresh and can be restored', async () => {
  const view = await mount([artifact('recoverable', 'prompt')])
  try {
    await view.click('删除资产：提示词')
    assert.equal(view.dialog('删除资产确认').open, true)
    await clickText(view, '取消')
    assert.equal(view.dialog('删除资产确认').open, false)
    assert.deepEqual(view.ids(), ['recoverable'])
    assert.equal(lifecycleWrites.length, 0)
    await view.click('删除资产：提示词')
    await clickText(view, '确认删除')
    assert.deepEqual(view.ids(), [])
    assert.equal(lifecycleWrites.length, 1)
    assert.ok(content({ text: '', children: view.nodes() }).includes('已移入回收站 1 项。'))
    await clickText(view, '刷新列表')
    assert.deepEqual(view.ids(), [])
    await clickText(view, '回收站')
    assert.deepEqual(view.ids(), ['recoverable'])
    await view.click('恢复资产：提示词'); await settle()
    assert.deepEqual(view.ids(), [])
    await clickText(view, '正常资产')
    assert.deepEqual(view.ids(), ['recoverable'])
    assert.deepEqual(lifecycleWrites.map(item => item.version), [1, 2])
  } finally { view.unmount() }
})

test('batch deletion selects current displayed results, reports partial failure and prevents duplicate submit', async () => {
  const view = await mount([artifact('ok', 'prompt'), artifact('stale', 'prompt'), artifact('hidden', 'image')])
  try {
    await view.click('提示词')
    await clickText(view, '全选当前显示')
    setLifecycleResponse('stale', new Error('资产状态已变化，请刷新列表后重试'))
    await clickText(view, '批量删除')
    const confirm = view.nodes().find(node => node.tag === 'button' && content(node).trim() === '确认删除')
    const first = confirm.props.onClick(); confirm.props.onClick(); await first; await settle()
    assert.deepEqual(lifecycleWrites.map(item => item.id), ['ok', 'stale'])
    assert.deepEqual(view.ids(), ['stale'])
    const text = view.nodes().map(content).join(' ')
    assert.ok(text.includes('已移入回收站 1 项，1 项未完成。'))
    assert.ok(text.includes('资产状态已变化'))
    assert.equal(view.nodes().find(node => node.tag === 'input' && node.props.type === 'checkbox').props.checked, true)
    await view.click('图片')
    assert.deepEqual(view.ids(), ['hidden'])
    assert.equal(view.nodes().find(node => node.tag === 'input' && node.props.type === 'checkbox').props.checked, false)
  } finally { view.unmount() }
})

test('batch selection never includes undisplayed results, and all failed responses never report success', async () => {
  const view = await mount(Array.from({ length: 55 }, (_, index) => artifact(`delete-${index}`, 'prompt')))
  try {
    await clickText(view, '全选当前显示')
    for (let index = 0; index < 48; index++) setLifecycleResponse(`delete-${index}`, new Error('服务暂时不可用'))
    await clickText(view, '批量删除')
    await clickText(view, '确认删除')
    assert.equal(lifecycleWrites.length, 48)
    assert.equal(view.ids().length, 48)
    assert.equal(view.nodes().some(node => node.props.role === 'status' && content(node).includes('已移入回收站')), false)
    assert.ok(view.nodes().some(node => node.tag === 'p' && content(node).includes('48 项未完成')))
  } finally { view.unmount() }
})

test('a response without the requested lifecycle state is an error rather than false deletion success', async () => {
  const value = artifact('wrong-response', 'prompt')
  const view = await mount([value])
  try {
    setLifecycleResponse(value.id, value)
    await view.click('删除资产：提示词')
    await clickText(view, '确认删除')
    assert.deepEqual(view.ids(), [value.id])
    assert.ok(view.nodes().some(node => content(node).includes('服务未确认资产状态')))
    assert.equal(view.nodes().some(node => node.props.role === 'status' && content(node).includes('已移入回收站')), false)
  } finally { view.unmount() }
})

test('recycle bin permanently deletes only the selected entries and reports the failed ones', async () => {
  const view = await mount([trashed('gone', 'image'), trashed('keep', 'prompt')])
  try {
    await clickText(view, '回收站')
    assert.deepEqual(view.ids(), ['gone', 'keep'])
    await clickText(view, '全选当前显示')
    setLifecycleResponse('keep', new Error('资产状态已变化，请刷新列表后重试'))
    await clickText(view, '删除')
    assert.equal(view.dialog('彻底删除资产确认').open, true)
    await clickDialog(view, '彻底删除资产确认', '确认彻底删除')
    assert.deepEqual(lifecycleWrites.map(item => [item.id, item.action]), [['gone', 'purge'], ['keep', 'purge']])
    assert.deepEqual(view.ids(), ['keep'])
    const text = view.nodes().map(content).join(' ')
    assert.ok(text.includes('已彻底删除 1 项，1 项未完成。'))
    assert.ok(text.includes('资产状态已变化'))
  } finally { view.unmount() }
})

test('recycle bin delete-all permanently removes every displayed entry', async () => {
  const view = await mount([trashed('a', 'image'), trashed('b', 'prompt')])
  try {
    await clickText(view, '回收站')
    await clickText(view, '全部删除')
    assert.equal(view.dialog('彻底删除资产确认').open, true)
    await clickDialog(view, '彻底删除资产确认', '确认彻底删除')
    assert.deepEqual(lifecycleWrites.map(item => item.id), ['a', 'b'])
    assert.deepEqual(view.ids(), [])
    assert.ok(view.nodes().some(node => node.text === '回收站为空'))
  } finally { view.unmount() }
})

test('cancelling a card purge sends no request and keeps the entry in the recycle bin', async () => {
  const view = await mount([trashed('cancel', 'image')])
  try {
    await clickText(view, '回收站')
    await view.click('彻底删除资产：图片')
    await clickDialog(view, '彻底删除资产确认', '取消')
    assert.equal(view.dialog('彻底删除资产确认').open, false)
    assert.deepEqual(view.ids(), ['cancel'])
    assert.equal(lifecycleWrites.length, 0)
  } finally { view.unmount() }
})

test('a purge response without the purged lifecycle state is an error rather than a false permanent delete', async () => {
  const value = trashed('wrong-purge', 'image')
  const view = await mount([value])
  try {
    setLifecycleResponse(value.id, { ...value, lifecycle_state: 'active', lifecycle_version: 3 })
    await clickText(view, '回收站')
    await view.click('彻底删除资产：图片')
    await clickDialog(view, '彻底删除资产确认', '确认彻底删除')
    assert.deepEqual(view.ids(), [value.id])
    assert.ok(view.nodes().some(node => content(node).includes('服务未确认资产状态')))
    assert.equal(view.nodes().some(node => node.props.role === 'status' && content(node).includes('已彻底删除')), false)
  } finally { view.unmount() }
})

test('reopening the recycle bin URL keeps the recycle bin selected and excludes active records', async () => {
  const values = [artifact('active-record', 'prompt'), trashed('trash-record', 'prompt')]
  const first = await mount(values)
  await clickText(first, '回收站')
  assert.equal(window.location.search, '?view=trash')
  assert.deepEqual(first.ids(), ['trash-record'])
  first.unmount()
  const restored = await mount(values)
  try {
    assert.deepEqual(restored.ids(), ['trash-record'])
    await clickText(restored, '正常资产')
    assert.equal(window.location.search, '')
    assert.deepEqual(restored.ids(), ['active-record'])
  } finally { restored.unmount() }
})

async function mountCard(value) {
  resetMedia(); clipboardFailure = false; clipboardText = ''; downloads.length = 0
  const selected = ref(value)
  const root = makeNode('root')
  const app = renderer.createApp({ setup: () => () => h(ArtifactCard, { artifact: selected.value, domain: 'comic' }) })
  app.mount(root); await settle()
  return {
    nodes: () => all(root),
    text: () => content(root),
    find: (tag, text = '') => all(root).find(node => node.tag === tag && content(node).trim() === text),
    dialog: () => all(root).find(node => node.tag === 'dialog'),
    async click(text) { const button = this.find('button', text); assert.ok(button, text); if (button.props.disabled) return; await button.props.onClick(); await settle() },
    async close() { this.dialog().close(); await settle() },
    async choose(value) { selected.value = value; await settle() },
    unmount: () => app.unmount(),
  }
}
function mediaArtifact(id, type = 'image', origin = 'real') {
  return { ...artifact(id, type, 'comic', origin), location: `offline/${id}.${type === 'image' ? 'png' : 'mp4'}` }
}

test('real image details show the image and a download, close releases the dialog URL and returns focus', async () => {
  const view = await mountCard(mediaArtifact('image-detail'))
  try {
    setMedia('artifact:image-detail', 'offline-image')
    await view.click('查看详情')
    assert.equal(view.dialog().open, true)
    const preview = view.dialog().children.flatMap(all).find(node => node.tag === 'img')
    assert.ok(preview?.props.src.startsWith('blob:'))
    const download = view.find('a', '下载原图')
    assert.equal(download.props.href, preview.props.src)
    assert.equal(download.props.download, '图片-v1.png')
    assert.equal(view.dialog().children.flatMap(all).find(node => node.tag === 'details').props.open, undefined)
    const url = preview.props.src
    await view.close()
    assert.ok(revoked.includes(url))
    assert.equal(toRaw(focused), view.find('button', '查看详情'))
    assert.equal(view.dialog().children.flatMap(all).some(node => node.tag === 'img'), false)
  } finally { view.unmount() }
})

test('real video details use native controls and download the video', async () => {
  const view = await mountCard(mediaArtifact('video-detail', 'video'))
  try {
    setMedia('artifact:video-detail', 'offline-video')
    await view.click('查看详情')
    const video = view.dialog().children.flatMap(all).find(node => node.tag === 'video')
    assert.equal(video.props.controls, true)
    assert.equal(view.find('a', '下载视频').props.href, video.props.src)
  } finally { view.unmount() }
})

test('legacy comic archive reuses the original image fallback and never invents real provenance', async () => {
  const value = { ...mediaArtifact('legacy'), source: 'comic.archive', metadata: { request_id: 'original-request', domain: 'comic' } }
  const view = await mountCard(value)
  try {
    setMedia('artifact:legacy', new Error('旧产物来源未登记'))
    setMedia('task:original-request', 'historical-image')
    await view.click('查看详情')
    assert.ok(view.find('a', '下载原图'))
    assert.ok(view.text().includes('来源未确认'))
    assert.ok(!view.text().includes('真实内容'))
    assert.deepEqual(calls.slice(-2), ['artifact:legacy', 'task:original-request'])
    assert.equal(value.metadata.origin, undefined)
  } finally { view.unmount() }
})

test('mock and blocked media never request files or offer a fake download', async () => {
  for (const origin of ['mock', 'blocked']) {
    const view = await mountCard(mediaArtifact(origin, 'image', origin))
    try {
      await view.click('查看详情')
      assert.equal(view.nodes().some(node => node.tag === 'img' || node.tag === 'a'), false)
      assert.equal(calls.length, 0)
      assert.ok(view.text().includes(origin === 'mock' ? '未生成真实图片' : '没有可交付的真实文件'))
    } finally { view.unmount() }
  }
})

test('media failures show the error identifier and rereading the same file can recover', async () => {
  const view = await mountCard(mediaArtifact('missing'))
  try {
    setMedia('artifact:missing', new Error('产物文件不可用 · 错误编号：offline-error'))
    await view.click('查看详情')
    assert.ok(view.text().includes('offline-error'))
    assert.equal(view.find('a', '下载原图'), undefined)
    setMedia('artifact:missing', 'recovered-file')
    const retry = view.dialog().children.flatMap(all).find(node => node.tag === 'button' && content(node).includes('重新读取'))
    await retry.props.onClick(); await settle()
    assert.ok(view.find('a', '下载原图'))
    assert.ok(!content(view.dialog()).includes('offline-error'))
  } finally { view.unmount() }
})

test('a decode failure removes the unusable media and its download', async () => {
  const view = await mountCard(mediaArtifact('broken'))
  try {
    setMedia('artifact:broken', 'corrupt-file')
    await view.click('查看详情')
    view.dialog().children.flatMap(all).find(node => node.tag === 'img').props.onError()
    await settle()
    assert.ok(content(view.dialog()).includes('文件可能损坏'))
    assert.equal(view.find('a', '下载原图'), undefined)
  } finally { view.unmount() }
})

test('complete comic prompt with negative constraints is visible, copied and exported without truncation', async () => {
  const positive = Array.from({ length: 30 }, (_, index) => `第${index + 1}段：人物与环境`).join('\n') + '\n<script>只是正文</script>'
  const expected = positive + '\n\nNegative Constraint / 禁止内容\n不要水印，保留完整约束。'
  const view = await mountCard({ ...artifact('prompt-detail', 'prompt'), source: 'comic.prompt.compiler', metadata: { positive_prompt: positive, negative_prompt: '不要水印，保留完整约束。', trace_id: 'technical-only' } })
  try {
    await view.click('查看详情')
    assert.ok(view.text().includes('第30段'))
    assert.ok(view.text().includes('保留完整约束。'))
    assert.equal(view.nodes().some(node => node.tag === 'script'), false)
    await view.click('复制完整内容')
    assert.equal(clipboardText, expected)
    assert.ok(view.text().includes('已复制完整内容'))
    await view.click('导出文本')
    assert.equal(downloads.at(-1).filename, '镜头提示词-v1.txt')
    assert.equal(await downloads.at(-1).blob.text(), expected)
  } finally { view.unmount() }
})

test('clipboard denial shows a failure without reporting copy success', async () => {
  const view = await mountCard({ ...artifact('copy-failure', 'prompt'), metadata: { content: '完整正文' } })
  try {
    await view.click('查看详情'); clipboardFailure = true
    await view.click('复制完整内容')
    assert.equal(clipboardText, '')
    assert.ok(view.text().includes('复制失败'))
    assert.ok(!view.text().includes('已复制完整内容'))
  } finally { view.unmount() }
})

test('empty content disables copy and export with an explicit empty state', async () => {
  const view = await mountCard(artifact('empty-document', 'document'))
  try {
    await view.click('查看详情')
    assert.ok(view.text().includes('尚无可展示的内容'))
    assert.equal(view.find('button', '复制完整内容').props.disabled, true)
    assert.equal(view.find('button', '导出文本').props.disabled, true)
  } finally { view.unmount() }
})

test('document and report details include every nested value instead of a five-row summary', async () => {
  for (const type of ['document', 'report']) {
    const view = await mountCard({ ...artifact(type, type), metadata: { payload: { title: '离线报告', content: '完整正文', one: 1, two: 2, three: 3, items: [{ title: '最后一个项目', details: { result: '末尾内容' } }] } } })
    try {
      await view.click('查看详情')
      assert.ok(content(view.dialog()).includes('末尾内容'))
      assert.ok(content(view.dialog()).includes('最后一个项目'))
      await view.click('复制完整内容')
      assert.ok(clipboardText.includes('末尾内容'))
      assert.ok(!clipboardText.includes('结构化内容'))
    } finally { view.unmount() }
  }
})

test('legacy commerce documents and listings use the existing domain renderers', async () => {
  const view = await mountCard({ ...artifact('legacy-document', 'document'), source: 'commerce.requirement', metadata: { mock: true, payload: { requirement: '旧版本的完整商品需求', locale: 'ru-RU' } } })
  try {
    assert.ok(view.text().includes('旧版本的完整商品需求'))
    await view.click('查看详情')
    assert.ok(content(view.dialog()).includes('目标语言 ru-RU'))
    assert.ok(view.text().includes('模拟数据（Mock）'))
  } finally { view.unmount() }
})

test('a response arriving after the detail closes is revoked and cannot reopen the result', async () => {
  const view = await mountCard(mediaArtifact('late'))
  try {
    let finish
    setMedia('artifact:late', () => new Promise(resolve => { finish = resolve }))
    await view.click('查看详情')
    assert.ok(view.text().includes('正在加载图片'))
    await view.close()
    const url = URL.createObjectURL(new Blob(['late-file']))
    finish(url); await settle()
    assert.ok(revoked.includes(url))
    assert.equal(view.dialog().open, false)
    assert.equal(view.dialog().children.flatMap(all).some(node => node.tag === 'img'), false)
  } finally { view.unmount() }
})

test('a missing origin has its own filter and is excluded from real content', async () => {
  const unknown = { ...artifact('unknown', 'prompt'), metadata: { content: '历史正文' } }
  const view = await mount([unknown, artifact('known-real', 'prompt', 'comic', 'real')])
  try {
    await view.choose('按来源状态筛选', 'real')
    assert.deepEqual(view.ids(), ['known-real'])
    await view.choose('按来源状态筛选', 'unknown')
    assert.deepEqual(view.ids(), ['unknown'])
  } finally { view.unmount() }
})
