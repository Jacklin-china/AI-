<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { FileText, Film, Image as ImageIcon, ListTree, X } from 'lucide-vue-next'
import type { CoreArtifact } from '../../types'
import { resolveArtifactRenderer } from './rendererRegistry'
import GenericArtifactContent from './GenericArtifactContent.vue'
import { assetDisplayName, artifactOrigin, artifactText, presentableArtifact } from './artifactPresentation'

const props = defineProps<{ artifact: CoreArtifact; domain: string; manageable?: boolean; selected?: boolean; busy?: boolean }>()
const emit = defineEmits<{ select: [boolean]; trash: []; restore: []; purge: [] }>()
const detailsOpen = ref(false)
const dialog = ref<HTMLDialogElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const feedback = ref('')
const actionError = ref('')
const copying = ref(false)
let detailSelection = 0
const displayArtifact = computed(() => presentableArtifact(props.artifact))
const schemaName = computed(() => String(displayArtifact.value.metadata.schema_name ?? 'generic'))
const name = computed(() => assetDisplayName(props.artifact, props.domain))
const renderer = computed(() => resolveArtifactRenderer(schemaName.value, props.artifact.type))
const media = computed(() => ['image', 'video'].includes(props.artifact.type))
const contentText = computed(() => artifactText(displayArtifact.value))
const origin = computed(() => artifactOrigin(props.artifact))
const originLabel = computed(() => ({ real: '真实内容', mock: '模拟数据（Mock）', blocked: '已阻止', unknown: '来源未确认' }[origin.value] ?? '来源未确认'))
const icon = computed(() =>
  ({ image: ImageIcon, video: Film, listing: ListTree } as Record<string, typeof FileText>)[props.artifact.type] ?? FileText,
)

async function openDetails(): Promise<void> {
  detailSelection++
  feedback.value = ''; actionError.value = ''
  detailsOpen.value = true
  await nextTick()
  dialog.value?.showModal()
}
function closeDetails(): void { dialog.value?.close() }
function closed(): void {
  detailSelection++
  detailsOpen.value = false
  copying.value = false
  feedback.value = ''; actionError.value = ''
  trigger.value?.focus()
}
async function copyContent(): Promise<void> {
  if (copying.value || !contentText.value.trim()) return
  copying.value = true
  const current = detailSelection
  feedback.value = ''; actionError.value = ''
  try {
    await navigator.clipboard.writeText(contentText.value)
    if (detailsOpen.value && current === detailSelection) feedback.value = '已复制完整内容。'
  } catch {
    if (detailsOpen.value && current === detailSelection) actionError.value = '复制失败，请手动选中正文复制，或使用导出文本。'
  } finally { if (current === detailSelection) copying.value = false }
}
function exportContent(): void {
  if (!contentText.value.trim()) return
  feedback.value = ''; actionError.value = ''
  let url = ''
  try {
    url = URL.createObjectURL(new Blob([contentText.value], { type: 'text/plain;charset=utf-8' }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${name.value.replace(/[<>:"/\\|?*\x00-\x1f]/g, '-')}-v${props.artifact.version}.txt`
    anchor.click()
    feedback.value = '已开始导出，请查看浏览器下载列表。'
  } catch {
    actionError.value = '导出未完成，请重试或复制正文。'
  } finally {
    if (url) window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
}
watch(() => props.artifact.id, () => { if (detailsOpen.value) closeDetails() })
onBeforeUnmount(() => { if (dialog.value?.open) dialog.value.close() })
</script>

<template>
  <article class="artifact-card" :data-type="artifact.type" :data-artifact-id="artifact.id">
    <header>
      <input v-if="manageable" type="checkbox" :checked="selected" :disabled="busy" :aria-label="`选择资产：${name}`" @change="emit('select', ($event.target as HTMLInputElement).checked)" />
      <i><component :is="icon" :size="14" :stroke-width="1.8" /></i>
      <div><strong>{{ name }}</strong><small>{{ new Date(artifact.created_at).toLocaleString() }}</small></div>
      <span class="origin-label" :data-origin="origin">{{ originLabel }}</span>
    </header>
    <div class="artifact-card-summary"><component :is="renderer" :artifact="displayArtifact" /></div>
    <footer>
      <span>版本 {{ artifact.version }}</span>
      <button ref="trigger" class="text-action" @click="openDetails">查看详情</button>
      <template v-if="manageable && artifact.lifecycle_state === 'trashed'">
        <button class="text-action" :disabled="busy" :aria-label="`恢复资产：${name}`" @click="emit('restore')">恢复</button>
        <button class="text-action asset-trash-action" :disabled="busy" :aria-label="`彻底删除资产：${name}`" @click="emit('purge')">彻底删除</button>
      </template>
      <button v-else-if="manageable" class="text-action asset-trash-action" :disabled="busy" :aria-label="`删除资产：${name}`" @click="emit('trash')">删除</button>
    </footer>
    <dialog ref="dialog" class="asset-detail-dialog" :aria-label="`${name}详情`" @close="closed" @click.self="closeDetails">
      <section v-if="detailsOpen" class="asset-detail-content">
        <header class="asset-detail-head"><h2>{{ name }}</h2><button class="ui-button quiet sm" aria-label="关闭资产详情" autofocus @click="closeDetails"><X :size="18" /></button></header>
        <p class="asset-detail-meta">{{ originLabel }} · 版本 {{ artifact.version }} · {{ new Date(artifact.created_at).toLocaleString() }}</p>
        <component :is="renderer" :artifact="displayArtifact" detailed />
        <section v-if="!media && renderer !== GenericArtifactContent" class="asset-full-record">
          <h3>完整内容</h3><GenericArtifactContent :artifact="displayArtifact" detailed />
        </section>
        <div v-if="!media" class="asset-detail-actions">
          <button class="ui-button sm" :disabled="!contentText.trim() || copying" @click="copyContent">{{ copying ? '正在复制…' : '复制完整内容' }}</button>
          <button class="ui-button sm" :disabled="!contentText.trim()" @click="exportContent">导出文本</button>
        </div>
        <p v-if="feedback" role="status">{{ feedback }}</p><p v-if="actionError" role="alert">{{ actionError }}</p>
        <details class="artifact-technical">
          <summary>技术详情</summary>
          <dl><div><dt>产物编号</dt><dd>{{ artifact.id }}</dd></div><div><dt>技术来源</dt><dd>{{ artifact.source }}</dd></div><div><dt>数据格式</dt><dd>{{ schemaName }}</dd></div></dl>
          <pre class="raw-json">{{ JSON.stringify(artifact.metadata, null, 2) }}</pre>
        </details>
      </section>
    </dialog>
  </article>
</template>

<style scoped>
.artifact-card-summary { max-height: 220px; overflow: hidden; }
.asset-trash-action { color: var(--danger); }
.asset-detail-dialog { width: min(800px, calc(100vw - 32px)); max-height: calc(100dvh - 48px); padding:0; border: 1px solid var(--border); border-radius: 12px; background: var(--surface, #fff); color: var(--text-primary); }
.asset-detail-dialog::backdrop { background: rgb(23 26 31 / .45); }
.asset-detail-content { padding:20px 24px; overflow-wrap: anywhere; }
.asset-detail-head { display: flex; justify-content: space-between; align-items: center; gap:12px; }
.asset-detail-head h2 { margin:0; font-size: 18px; }
.asset-detail-meta { font-size: 12px; color: var(--text-secondary); margin:12px 0 20px; }
.asset-detail-actions { display: flex; flex-wrap: wrap; gap:8px; margin-top:20px; }
.asset-full-record { margin-top:20px; border-top: 1px solid var(--border); padding-top:12px; }
.asset-full-record h3 { font-size: 14px; }
.artifact-technical { margin-top:20px; }
.artifact-technical summary { cursor: pointer; font-size: 12px; color: var(--text-secondary); }
.artifact-technical dd { overflow-wrap: anywhere; }
.raw-json { white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 600px) { .asset-detail-content { padding:16px; } }
</style>
