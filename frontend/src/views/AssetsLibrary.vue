<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { FileText, Image as ImageIcon, Images } from 'lucide-vue-next'
import EmptyState from '../components/EmptyState.vue'
import WorkspacePage from '../components/workspace/WorkspacePage.vue'
import ArtifactCard from '../components/artifacts/ArtifactCard.vue'
import { changeArtifactLifecycle, getArtifacts, getRuns } from '../services/core'
import type { CoreArtifact, CoreRun } from '../types'
import { assetDisplayName, artifactOrigin } from '../components/artifacts/artifactPresentation'

const shellNavigation = [
  { id: 'assets', label: '全部产物', icon: Images },
  { id: 'image', label: '图片', icon: ImageIcon },
  { id: 'prompt', label: '提示词', icon: FileText },
]
const artifacts = ref<CoreArtifact[]>([])
const runs = ref<CoreRun[]>([])
const typeFilter = ref('all')
const domainFilter = ref('all')
const originFilter = ref('all')
const timeFilter = ref('all')
const limit = ref(48)
const lifecycle = ref<'active' | 'trashed'>(new URLSearchParams(window.location?.search ?? '').get('view') === 'trash' ? 'trashed' : 'active')
const selected = ref<string[]>([])
const loading = ref(false)
const busy = ref(false)
const feedback = ref('')
const loadError = ref('')
const failures = ref<{ id: string; name: string; reason: string }[]>([])
const confirmation = ref<HTMLDialogElement | null>(null)
const purgeConfirmation = ref<HTMLDialogElement | null>(null)
const pending = ref<CoreArtifact[]>([])
const pendingPurge = ref<CoreArtifact[]>([])
let refreshSelection = 0
let mounted = true
/** 资产库只保留图片与提示词；其他类型仅隐藏，数据与工作区引用仍保留。 */
const assetTypes = ['image', 'prompt']
const actionVerbs = { trash: '移入回收站', restore: '恢复', purge: '彻底删除' } as const
const actionStates = { trash: 'trashed', restore: 'active', purge: 'purged' } as const
const activeCategory = computed(() => (assetTypes.includes(typeFilter.value) ? typeFilter.value : 'assets'))

function selectCategory(page: string): void {
  typeFilter.value = assetTypes.includes(page) ? page : 'all'
  limit.value = 48
}

async function refresh(): Promise<void> {
  const current = ++refreshSelection
  loading.value = true; loadError.value = ''
  try {
    const [artifactData, runData] = await Promise.all([getArtifacts(undefined, lifecycle.value), getRuns()])
    if (!mounted || current !== refreshSelection) return
    if (artifactData) artifacts.value = artifactData
    if (runData) runs.value = runData
  } catch (error) {
    if (mounted && current === refreshSelection) loadError.value = error instanceof Error ? error.message : '资产读取失败，请重试。'
  } finally { if (mounted && current === refreshSelection) loading.value = false }
}

async function switchLifecycle(value: 'active' | 'trashed'): Promise<void> {
  if (busy.value || lifecycle.value === value) return
  lifecycle.value = value; artifacts.value = []; selected.value = []; limit.value = 48
  const url = new URL(window.location.href)
  if (value === 'trashed') url.searchParams.set('view', 'trash')
  else url.searchParams.delete('view')
  window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash)
  feedback.value = ''; failures.value = []
  await refresh()
}
function selectArtifact(id: string, checked: boolean): void {
  if (busy.value) return
  selected.value = checked ? [...new Set([...selected.value, id])] : selected.value.filter(value => value !== id)
}
const allDisplayedSelected = computed(() => displayed.value.length > 0 && displayed.value.every(item => selected.value.includes(item.id)))
const selectedItems = computed(() => displayed.value.filter(item => selected.value.includes(item.id)))
function selectDisplayed(): void {
  if (busy.value) return
  selected.value = allDisplayedSelected.value ? [] : displayed.value.map(item => item.id)
}
async function confirmTrash(items: CoreArtifact[]): Promise<void> {
  if (busy.value || !items.length) return
  pending.value = items.map(item => ({ ...item }))
  await nextTick()
  confirmation.value?.showModal()
}
function cancelTrash(): void { confirmation.value?.close() }
function confirmationClosed(): void { pending.value = [] }
async function executeTrash(): Promise<void> {
  if (busy.value || !pending.value.length) return
  const items = pending.value
  confirmation.value?.close()
  await changeItems(items, 'trash')
}
async function confirmPurge(items: CoreArtifact[]): Promise<void> {
  if (busy.value || !items.length) return
  pendingPurge.value = items.map(item => ({ ...item }))
  await nextTick()
  purgeConfirmation.value?.showModal()
}
function cancelPurge(): void { purgeConfirmation.value?.close() }
function purgeConfirmationClosed(): void { pendingPurge.value = [] }
async function executePurge(): Promise<void> {
  if (busy.value || !pendingPurge.value.length) return
  const items = pendingPurge.value
  purgeConfirmation.value?.close()
  await changeItems(items, 'purge')
}
async function changeItems(items: CoreArtifact[], action: 'trash' | 'restore' | 'purge'): Promise<void> {
  if (busy.value) return
  busy.value = true; feedback.value = ''; failures.value = []
  let succeeded = 0
  const errors: { id: string; name: string; reason: string }[] = []
  try {
    for (const item of items) {
      try {
        const updated = await changeArtifactLifecycle(item, action)
        if (updated.id !== item.id || updated.lifecycle_state !== actionStates[action]) throw new Error('服务未确认资产状态，请刷新核实后重试。')
        succeeded++
        if (mounted) {
          artifacts.value = artifacts.value.filter(value => value.id !== item.id)
          selected.value = selected.value.filter(id => id !== item.id)
        }
      } catch (error) {
        errors.push({ id: item.id, name: assetDisplayName(item, domainOf(item)), reason: error instanceof Error ? error.message : '操作未完成，请刷新核实。' })
      }
    }
    if (mounted) {
      failures.value = errors
      feedback.value = succeeded ? `已${actionVerbs[action]} ${succeeded} 项${errors.length ? `，${errors.length} 项未完成。` : '。'}` : ''
    }
  } finally { if (mounted) busy.value = false }
}
watch([typeFilter, domainFilter, originFilter, timeFilter], () => { selected.value = []; limit.value = 48 })
onBeforeUnmount(() => {
  mounted = false; refreshSelection++
  confirmation.value?.close(); purgeConfirmation.value?.close()
})

const domainOf = (artifact: CoreArtifact): string => {
  const schema = String(artifact.metadata.schema_name ?? '')
  const prefix = schema.split('.')[0] || artifact.source.split('.')[0]
  if (['commerce', 'comic', 'studio'].includes(prefix)) return prefix
  return runs.value.find((run) => run.id === artifact.run_id)?.domain ?? 'studio'
}

const visible = computed(() => artifacts.value.filter((item) => {
  const origin = artifactOrigin(item)
  // 资产库只保留图片与提示词：其余类型一律不进入列表，也不进入全选范围。
  if (!assetTypes.includes(item.type)) return false
  if (typeFilter.value !== 'all' && item.type !== typeFilter.value) return false
  if (domainFilter.value !== 'all' && domainOf(item) !== domainFilter.value) return false
  if (originFilter.value !== 'all' && origin !== originFilter.value) return false
  if (timeFilter.value !== 'all') {
    const age = Date.now() - new Date(item.created_at).getTime()
    if (age > Number(timeFilter.value) * 86_400_000) return false
  }
  return true
}))
const displayed = computed(() => visible.value.slice(0, limit.value))

onMounted(() => void refresh())
</script>

<template>
  <WorkspacePage identity="资产" subtitle="全局历史产物" :active-page="activeCategory" :navigation="shellNavigation" @select-page="selectCategory">
  <main class="page assets-page">
    <header class="page-header">
      <div>
        <span class="section-kicker">全局历史产物</span>
        <h1>资产</h1>
        <p>资产库只保留图片与提示词。删除后可在回收站恢复，作品引用和历史内容会保留；在回收站彻底删除会同时删除磁盘文件，且不可恢复。</p>
      </div>
      <div class="asset-filters">
        <select v-model="domainFilter" :disabled="busy" class="ui-select" aria-label="按领域筛选">
          <option value="all">全部领域</option><option value="commerce">Commerce</option><option value="comic">Comic</option><option value="studio">Studio</option>
        </select>
        <select v-model="typeFilter" :disabled="busy" class="ui-select" aria-label="按类型筛选">
          <option value="all">全部类型</option>
          <option value="image">图片</option>
          <option value="prompt">提示词</option>
        </select>
        <select v-model="originFilter" :disabled="busy" class="ui-select" aria-label="按来源状态筛选">
          <option value="all">全部来源</option><option value="real">真实内容</option><option value="mock">模拟数据（Mock）</option><option value="blocked">已阻止</option><option value="unknown">来源未确认</option>
        </select>
        <select v-model="timeFilter" :disabled="busy" class="ui-select" aria-label="按时间筛选">
          <option value="all">全部时间</option><option value="7">最近 7 天</option><option value="30">最近 30 天</option>
        </select>
      </div>
    </header>

    <div class="asset-management">
      <button class="ui-button sm" :aria-pressed="lifecycle === 'active'" :disabled="busy" @click="switchLifecycle('active')">正常资产</button>
      <button class="ui-button sm" :aria-pressed="lifecycle === 'trashed'" :disabled="busy" @click="switchLifecycle('trashed')">回收站</button>
      <button class="ui-button sm" :disabled="busy || loading" @click="selected = []; refresh()">刷新列表</button>
      <button v-if="displayed.length" class="ui-button sm" :disabled="busy || loading" @click="selectDisplayed">{{ allDisplayedSelected ? '取消全选' : '全选当前显示' }}</button>
      <span v-if="selected.length">已选 {{ selected.length }} 项</span>
      <template v-if="lifecycle === 'active'">
        <button class="ui-button sm" :disabled="!selected.length || busy || loading" @click="confirmTrash(selectedItems)">批量删除</button>
      </template>
      <template v-else>
        <button class="ui-button sm" :disabled="!selected.length || busy || loading" @click="changeItems(selectedItems, 'restore')">恢复所选</button>
        <button class="ui-button sm" :disabled="!selected.length || busy || loading" @click="confirmPurge(selectedItems)">删除</button>
        <button class="ui-button sm" :disabled="!displayed.length || busy || loading" @click="confirmPurge(displayed)">全部删除</button>
      </template>
    </div>
    <p v-if="busy" role="status">正在处理资产，请稍候…</p>
    <p v-if="loading" role="status">正在读取资产…</p>
    <p v-if="feedback" role="status">{{ feedback }}</p>
    <p v-if="loadError" role="alert">{{ loadError }} <button class="text-action" :disabled="busy || loading" @click="refresh">重新读取列表</button></p>
    <div v-if="failures.length" role="alert"><p>{{ failures.length }} 项未完成，请刷新核实后重试：</p><ul><li v-for="failure in failures" :key="failure.id">{{ failure.name }}：{{ failure.reason }}</li></ul></div>
    <p v-if="visible.length" class="asset-result-count">显示 {{ displayed.length }} / {{ visible.length }} 个产物；全选仅包括当前显示的 {{ displayed.length }} 项</p>
    <section v-if="visible.length" class="asset-grid">
      <ArtifactCard
        v-for="item in displayed"
        :key="item.id"
        :artifact="item"
        :domain="domainOf(item)"
        manageable
        :selected="selected.includes(item.id)"
        :busy="busy || loading"
        @select="selectArtifact(item.id, $event)"
        @trash="confirmTrash([item])"
        @restore="changeItems([item], 'restore')"
        @purge="confirmPurge([item])"
      />
    </section>
    <button v-if="displayed.length < visible.length" class="ui-button asset-load-more" @click="limit += 48">加载更多</button>
    <EmptyState v-if="!visible.length && !loading && !loadError" :title="lifecycle === 'trashed' ? '回收站为空' : '暂无产物'" :description="lifecycle === 'trashed' ? '已删除的资产会在这里保留，可随时恢复。' : '任务产物会进入这里，也可调整筛选条件。'" />
    <dialog ref="confirmation" class="asset-trash-dialog" aria-label="删除资产确认" @close="confirmationClosed">
      <h2>删除 {{ pending.length }} 项资产？</h2>
      <p>这些资产将移入回收站，可随时恢复。媒体文件、作品引用和历史记录会保留。</p>
      <ul><li v-for="item in pending.slice(0, 10)" :key="item.id">{{ assetDisplayName(item, domainOf(item)) }} · 版本 {{ item.version }} · {{ new Date(item.created_at).toLocaleString() }}</li></ul>
      <p v-if="pending.length > 10">另有 {{ pending.length - 10 }} 项。</p>
      <div class="asset-management"><button class="ui-button sm" autofocus @click="cancelTrash">取消</button><button class="ui-button sm" :disabled="busy || !pending.length" @click="executeTrash">确认删除</button></div>
    </dialog>
    <dialog ref="purgeConfirmation" class="asset-trash-dialog" aria-label="彻底删除资产确认" @close="purgeConfirmationClosed">
      <h2>彻底删除 {{ pendingPurge.length }} 项资产？</h2>
      <p>彻底删除会同时删除磁盘上的媒体文件，无法恢复，也不能再从回收站取回。历史记录会保留但不再显示该资产。</p>
      <ul><li v-for="item in pendingPurge.slice(0, 10)" :key="item.id">{{ assetDisplayName(item, domainOf(item)) }} · 版本 {{ item.version }} · {{ new Date(item.created_at).toLocaleString() }}</li></ul>
      <p v-if="pendingPurge.length > 10">另有 {{ pendingPurge.length - 10 }} 项。</p>
      <div class="asset-management"><button class="ui-button sm" autofocus @click="cancelPurge">取消</button><button class="ui-button sm" :disabled="busy || !pendingPurge.length" @click="executePurge">确认彻底删除</button></div>
    </dialog>
  </main>
  </WorkspacePage>
</template>

<style scoped>
.asset-management { display: flex; flex-wrap: wrap; align-items: center; gap:8px; margin-bottom:16px; font-size: 13px; }
.asset-management [aria-pressed="true"] { background: var(--sidebar-selected); }
.asset-trash-dialog { width: min(480px, calc(100vw - 32px)); max-height: calc(100dvh - 48px); overflow: auto; padding:24px; border: 1px solid var(--border); border-radius: 12px; color: var(--text-primary); background: var(--surface); }
.asset-trash-dialog::backdrop { background: rgb(23 26 31 / .45); }
.asset-trash-dialog h2 { margin-top:0; font-size: 18px; }
.asset-trash-dialog p, .asset-trash-dialog li { font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
</style>
