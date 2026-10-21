<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { AlertCircle, Check, Copy, Download, GripVertical, Plus, RefreshCw, Trash2 } from 'lucide-vue-next'
import type { ComicStoryShot } from '../../services/core'
import { storyCodeLabels, storyFieldLabels, storyImageKey, type StoryWorkbench } from '../../domains/comic/storyWorkbench'
import ImageViewport from '../ui/ImageViewport.vue'

// DirectorWorkspace owns one controller per conversation across story/export pages.
const props = defineProps<{ stage: 'story' | 'export'; workbench: StoryWorkbench }>()
const emit = defineEmits<{ copy: [text: string, hint: string]; go: [page: string]; repair: [projectId: string] }>()
const state = computed(() => props.workbench.state)
const operationBusy = computed(() => !!state.value.operation || state.value.loading || !!state.value.unknownRequest)
const copied = ref(false), copyFailed = ref(false), restoreChoice = ref<number | null>(null)
const draggedShot = ref(''), historyOpen = ref(false), candidatePage = ref(0)
const stageRoot = ref<HTMLElement | null>(null)
const largeImage = ref<{ src: string; alt: string } | null>(null)
const imageDialog = ref<HTMLElement | null>(null)
/* 放大预览是全屏层：它必须挂在 body 上，否则会被 .stage-scroll 这个滚动容器裁剪，
   关闭入口被页眉盖住、空白处也点不到（用户因此无法返回故事编排）。 */
let restoreFocus: HTMLElement | null = null
let restoreScroll: { target: HTMLElement; top: number } | null = null
function scrollHost(element: HTMLElement | null): HTMLElement | null {
  let node = element?.parentElement ?? null
  while (node && node !== document.body) {
    const overflow = getComputedStyle(node).overflowY
    if (overflow === 'auto' || overflow === 'scroll') return node
    node = node.parentElement
  }
  return null
}
function openImage(image: { artifact_id: string; filename: string } | undefined): void {
  const src = image && state.value.previews[image.artifact_id]
  if (!image || !src) return
  restoreFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  const host = scrollHost(stageRoot.value)
  restoreScroll = host ? { target: host, top: host.scrollTop } : null
  largeImage.value = { src, alt: image.filename }
  void nextTick(() => imageDialog.value?.focus())
}
function closeImage(): void {
  if (!largeImage.value) return
  largeImage.value = null
  const focus = restoreFocus, scroll = restoreScroll
  restoreFocus = null; restoreScroll = null
  void nextTick(() => {
    // 关闭预览等于「回到进入放大前的界面」：滚动位置与焦点都不迁移。
    if (scroll?.target.isConnected) scroll.target.scrollTop = scroll.top
    focus?.focus?.({ preventScroll: true })
  })
}
/* Esc 挂到 window 上：焦点可能因为点到外壳而离开弹层，绑在弹层自身上会失效。 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape' || !largeImage.value) return
  event.preventDefault(); event.stopPropagation(); closeImage()
}
watch(largeImage, value => {
  if (typeof window === 'undefined') return
  if (value) window.addEventListener('keydown', onKeydown, true)
  else window.removeEventListener('keydown', onKeydown, true)
})
onBeforeUnmount(() => { if (typeof window !== 'undefined') window.removeEventListener('keydown', onKeydown, true) })
watch(() => state.value.owner, () => { closeImage() })
const shownImages = computed(() => state.value.images.slice(candidatePage.value * 8, candidatePage.value * 8 + 8))
const review = computed(() => props.workbench.dirty.value ? null : state.value.view?.review)
/* 校验结果默认收成「摘要 + 第一条」，完整清单靠展开按钮：
   首屏要留给可编辑的故事与镜头，而不是把编辑区挤出屏幕（审查报告 I3）。 */
const findingsOpen = ref(false)
const reviewFindings = computed(() => review.value?.findings ?? [])
const visibleFindings = computed(() => findingsOpen.value ? reviewFindings.value : reviewFindings.value.slice(0, 1))
const title = computed(() => props.stage === 'story' ? '故事编排' : '导出材料')
// 按钮直接说明这次会用哪些输入：勾选图片 / 已填镜头框架 / 两者都空时的禁用态。
const generateLabel = computed(() => state.value.operation === 'generate' ? (state.value.saving ? '正在保存并准备初稿' : '正在整理初稿')
  : state.value.selectedImages.length ? `AI 整理初稿（已选 ${state.value.selectedImages.length} 张）`
    : state.value.draft.shots.length ? `AI 整理初稿（按已填 ${state.value.draft.shots.length} 个镜头）`
      : 'AI 整理初稿')
function imageFor(shot: Pick<ComicStoryShot, 'image'>) {
  const image = state.value.view?.story?.fixed_images?.find(image => storyImageKey(image) === storyImageKey(shot.image))
    ?? state.value.images.find(image => storyImageKey(image) === storyImageKey(shot.image))
  const missing = state.value.view?.missing_images.find(item => item.image_id === shot.image.image_id && item.image_version === shot.image.version)
  return image && missing ? { ...image, available: false, unavailable_reason: missing.reason } : image
}
function exportImage(shot: { image_id: string; image_version: number }) {
  return imageFor({ image: { image_id: shot.image_id, version: shot.image_version } })
}
const visibleImages = computed(() => {
  const images = props.stage === 'story'
    ? [...state.value.draft.shots.map(imageFor), ...shownImages.value]
    : (state.value.view?.manifest?.shots ?? []).map(exportImage)
  return [...new Map(images.filter(image => !!image).map(image => [image!.artifact_id, image!])).values()]
})
watch(() => [props.stage, ...visibleImages.value.map(image => image.artifact_id + ':' + image.available)], () => {
  void props.workbench.showImages(visibleImages.value)
}, { immediate: true })
function shotNumber(id: string | null): string {
  const index = state.value.draft.shots.findIndex(shot => shot.shot_id === id); return index >= 0 ? `镜头 ${index + 1}` : '故事'
}
function shortText(value: string, limit = 80): string {
  const text = value.replace(/\s+/g, ' ').trim()
  return text.length > limit ? text.slice(0, limit) + '…' : text
}
function missingFor(shot: ComicStoryShot): string {
  const image = state.value.images.find(item => storyImageKey(item) === storyImageKey(shot.image))
  return state.value.view?.missing_images.find(item => item.shot_id === shot.shot_id)?.reason
    ?? (image?.available === false ? image.unavailable_reason || '图片已不可用，请选择有效图片。' : '')
}
function shotKey(event: KeyboardEvent, index: number): void {
  if (!event.altKey || !['ArrowUp', 'ArrowDown'].includes(event.key)) return
  event.preventDefault(); props.workbench.moveShot(index, index + (event.key === 'ArrowUp' ? -1 : 1))
}
function startDrag(event: DragEvent, shot: ComicStoryShot): void {
  draggedShot.value = shot.shot_id; event.dataTransfer?.setData('text/plain', shot.shot_id)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
}
function dropShot(index: number): void {
  const source = state.value.draft.shots.findIndex(shot => shot.shot_id === draggedShot.value)
  if (source >= 0) props.workbench.moveShot(source, index); draggedShot.value = ''
}
function changeDuration(shot: ComicStoryShot, event: Event): void {
  const value = (event.target as HTMLInputElement).value
  shot.duration_seconds = value === '' ? null : Number(value); props.workbench.changed()
}
async function copyText(): Promise<void> {
  const text = state.value.view?.export_text
  if (!props.workbench.canExport.value || !text) return
  copied.value = false; copyFailed.value = false
  try { await navigator.clipboard.writeText(text); copied.value = true }
  catch { copyFailed.value = true; emit('copy', text, '视频制作文字') }
}
async function showPage(step: number): Promise<void> {
  candidatePage.value += step
}
async function loadShotPreview(shot: Pick<ComicStoryShot, 'image'>): Promise<void> { const image = imageFor(shot); if (image) await props.workbench.preview(image, true) }
</script>

<template>
  <section ref="stageRoot" class="flow-stage" :aria-label="title">
    <Teleport to="body">
      <div v-if="largeImage" ref="imageDialog" class="flow-large-image" role="dialog" aria-modal="true" aria-label="图片大图预览" tabindex="-1" @wheel.prevent @click.self="closeImage">
        <header class="flow-large-bar">
          <small :title="largeImage.alt">{{ largeImage.alt }}</small>
          <button type="button" class="ui-button flow-large-close" @click="closeImage">关闭预览<span class="flow-large-key">Esc</span></button>
        </header>
        <div class="flow-large-canvas"><ImageViewport :src="largeImage.src" :alt="largeImage.alt" @empty="closeImage" /></div>
        <p class="flow-large-hint">滚轮缩放 · 拖动平移 · 点击空白处或按 Esc 关闭预览，回到原页面</p>
      </div>
    </Teleport>
    <header class="flow-stage-head"><div><h3>{{ title }}</h3><small>{{ stage === 'story' ? '选择本作品已采用的图片，编排故事与镜头。图片版本随故事固定。' : '复制完整制作文字，并下载同一已确认故事版本的原始图片。' }}</small></div><span class="flow-save" role="status" aria-live="polite">{{ workbench.saveLabel.value }}</span></header>
    <div v-if="state.error" class="flow-notice is-error" role="alert"><AlertCircle :size="15" /><div><p>{{ state.error }}</p><button v-if="state.conflict" class="ui-button sm" :disabled="state.saving" @click="workbench.save(true)">保存本地修改为新版本</button><button v-else-if="state.unknownRequest" class="ui-button sm" :disabled="!!state.operation" @click="workbench.recoverRequest()">查询原请求结果</button><button v-else-if="state.dirty" class="ui-button sm" :disabled="state.saving" @click="workbench.save()">重新保存修改</button><button v-else class="ui-button sm" @click="workbench.load()">重新读取</button></div></div>
    <p v-if="state.notice" class="flow-notice" role="status">{{ state.notice }}</p>
    <div v-if="state.imageWarnings.length" class="flow-notice is-error" role="status"><div><p v-for="(image, index) in state.imageWarnings" :key="storyImageKey(image)">已采用图片 {{ index + 1 }} · v{{ image.version }} 不可用：{{ image.reason }}</p><button class="ui-button sm" @click="emit('go', 'images')">到图片工作台选择有效版本</button></div></div>
    <div v-if="state.unknownRequest && !state.error" class="flow-notice" role="status"><p>原请求结果尚未确定。</p><button class="ui-button sm" @click="workbench.recoverRequest()">查询原请求结果</button></div>
    <section v-if="review?.validation_requested && review.findings.length" id="story-validation-results" class="flow-notice flow-validation" :class="{ 'is-error': review.status === 'needs_revision' }" :role="review.status === 'needs_revision' ? 'alert' : 'status'" aria-label="故事校验结果" tabindex="-1">
      <AlertCircle :size="15" />
      <div class="flow-validation-body"><strong>{{ review.status === 'needs_revision' ? `发现 ${review.findings.length} 处需要修改` : '校验通过，有优化建议' }}</strong>
        <ul class="flow-list flow-validation-list"><li v-for="(finding, index) in visibleFindings" :key="index">
          <strong>{{ shotNumber(finding.shot_id) }} · {{ storyFieldLabels[finding.field] || finding.field }}<small>{{ storyCodeLabels[finding.code ?? 'other'] || '其他问题' }}</small></strong>
          <div><p>{{ shortText(finding.message) }}</p><p v-if="finding.suggestion" class="flow-fix">建议：{{ shortText(finding.suggestion, 100) }}</p>
            <details v-if="finding.message.length > 80 || (finding.suggestion?.length ?? 0) > 100"><summary>展开完整说明</summary><p>{{ finding.message }}</p><p v-if="finding.suggestion">建议：{{ finding.suggestion }}</p></details>
          </div>
        </li></ul>
        <button v-if="reviewFindings.length > 1" type="button" class="ui-button quiet sm flow-validation-more"
          :aria-expanded="findingsOpen" aria-controls="story-validation-results" @click="findingsOpen = !findingsOpen">
          {{ findingsOpen ? '收起修改建议' : `展开全部 ${reviewFindings.length} 条` }}
        </button>
      </div>
    </section>
    <template v-if="stage === 'story'">
      <div class="flow-columns">
        <section class="flow-col flow-editor" aria-label="故事编辑">
          <label>故事标题<input v-model="state.draft.title" :disabled="state.loading" maxlength="200" placeholder="为这个故事命名" @input="workbench.changed()" /></label>
          <label>故事概述<textarea v-model="state.draft.summary" :disabled="state.loading" rows="3" placeholder="故事发生了什么，镜头之间如何衔接" @input="workbench.changed()" /></label>
          <div class="flow-col-head"><strong>镜头顺序 <small>{{ state.draft.shots.length }} 个镜头</small></strong><span>拖动手柄排序 · Alt + ↑ / ↓</span></div>
          <ol v-if="state.draft.shots.length" class="flow-shots">
            <li v-for="(shot, index) in state.draft.shots" :key="shot.shot_id" :data-story-shot="shot.shot_id" @dragover.prevent @drop.prevent="dropShot(index)">
              <header><button class="ui-button quiet sm flow-grip" draggable="true" :aria-label="`移动镜头 ${index + 1}`" title="拖动或按 Alt + ↑ / ↓ 调整顺序" @dragstart="startDrag($event, shot)" @dragend="draggedShot = ''" @keydown="shotKey($event, index)"><GripVertical :size="16" /></button><strong>镜头 {{ index + 1 }}</strong><span class="flow-shot-actions"><button class="ui-button quiet sm" :disabled="index === 0" :aria-label="`上移镜头 ${index + 1}`" @click="workbench.moveShot(index, index - 1)">↑</button><button class="ui-button quiet sm" :disabled="index === state.draft.shots.length - 1" :aria-label="`下移镜头 ${index + 1}`" @click="workbench.moveShot(index, index + 1)">↓</button><button class="ui-button quiet sm" :aria-label="`移除镜头 ${index + 1}`" @click="workbench.removeShot(index)"><Trash2 :size="14" /></button></span></header>
              <div class="flow-shot-content"><div class="flow-shot-image">
                <img v-if="imageFor(shot) && state.previews[imageFor(shot)!.artifact_id]" :src="state.previews[imageFor(shot)!.artifact_id]" :alt="`镜头 ${index + 1} 使用的图片`" role="button" tabindex="0" @click="openImage(imageFor(shot))" @keydown.enter="openImage(imageFor(shot))" @error="workbench.previewFailed(imageFor(shot)!)" /><div v-else class="flow-image-status" role="status"><small>{{ imageFor(shot) ? state.previewErrors[imageFor(shot)!.artifact_id] || '正在读取图片' : '图片暂不可读取' }}</small><button v-if="imageFor(shot) && state.previewErrors[imageFor(shot)!.artifact_id]" class="ui-button quiet sm" @click="loadShotPreview(shot)">重新读取图片</button></div><small>{{ imageFor(shot)?.filename ?? shot.image.image_id }} · v{{ shot.image.version }}</small>
                <label>替换图片<select :value="storyImageKey(shot.image)" :aria-label="`镜头 ${index + 1} 的图片`" @change="workbench.replaceImage(index, ($event.target as HTMLSelectElement).value)"><option v-if="!state.images.some(image => storyImageKey(image) === storyImageKey(shot.image))" :value="storyImageKey(shot.image)">{{ imageFor(shot)?.filename ?? '已固定图片' }} · v{{ shot.image.version }}</option><option v-for="image in state.images" :key="storyImageKey(image)" :value="storyImageKey(image)" :disabled="image.available === false">{{ image.label || image.filename }} · v{{ image.version }}{{ image.available === false ? "（不可用）" : "" }}</option></select></label>
                <p v-if="missingFor(shot)" class="flow-error">{{ missingFor(shot) }}</p><button v-if="workbench.imageReplacement(shot)" class="ui-button sm" :disabled="operationBusy" @click="workbench.repairImage(index)">使用已采用的同图修复引用</button><button v-else-if="missingFor(shot)" class="ui-button sm" @click="emit('repair', imageFor(shot)?.project_id ?? '')">到图片工作台重传</button>
              </div><div class="flow-shot-fields"><label>动作<textarea v-model="shot.action" rows="2" :aria-label="`镜头 ${index + 1} 的动作`" placeholder="人物或画面如何运动" @input="workbench.changed()" /></label><label>运镜<input v-model="shot.camera" :aria-label="`镜头 ${index + 1} 的运镜`" placeholder="描述相机运动" @input="workbench.changed()" /></label><div class="flow-field-pair"><label>时长（秒）<input type="number" min="0.1" max="60" step="0.1" :value="shot.duration_seconds ?? ''" :aria-label="`镜头 ${index + 1} 的时长`" @input="changeDuration(shot, $event)" /></label><label>转场<input v-model="shot.transition" :aria-label="`镜头 ${index + 1} 的转场`" placeholder="与下一镜如何衔接" @input="workbench.changed()" /></label></div></div></div>
            </li>
          </ol><p v-else class="pane-note">还没有镜头。先从右侧已采用图片中添加，或勾选图片让 AI 整理初稿。</p>
        </section>
        <aside class="flow-col flow-side" aria-label="选图与校验">
          <section><div class="flow-col-head"><strong>本作品的已采用图片</strong><button class="ui-button quiet sm" :disabled="state.loading" aria-label="刷新已采用图片" @click="workbench.load()"><RefreshCw :size="14" /></button></div>
            <ul v-if="state.images.length" class="flow-images"><li v-for="image in shownImages" :key="storyImageKey(image)"><label class="flow-image-pick"><input type="checkbox" :disabled="image.available === false" :checked="state.selectedImages.includes(storyImageKey(image))" :aria-label="`选入 ${image.filename} v${image.version}`" @change="workbench.toggleImage(image)" /><img v-if="state.previews[image.artifact_id]" :src="state.previews[image.artifact_id]" :alt="image.filename" @error="workbench.previewFailed(image)" /><span v-else class="flow-thumb-missing">{{ state.previewErrors[image.artifact_id] ? '图片暂不可读取' : '正在读取图片' }}</span><span><strong :title="image.label || image.filename">{{ image.label || image.filename }}</strong><small>v{{ image.version }} · {{ image.source_project_title || '本作品作品' }}</small><small v-if="image.available === false" class="flow-error">{{ image.unavailable_reason || '图片不可用，请到图片工作台选择有效版本' }}</small></span></label><button class="ui-button sm" :disabled="image.available === false" :aria-label="`添加 ${image.filename} 为镜头`" @click="workbench.addImage(image)"><Plus :size="13" /> 添加镜头</button></li></ul><p v-else class="pane-note">本作品尚无已采用图片。在图片工作台查看图片并点击「采用此版本」。</p>
            <div v-if="state.images.length > 8" class="flow-page"><button class="ui-button quiet sm" :disabled="candidatePage === 0" @click="showPage(-1)">上一页</button><small>{{ candidatePage + 1 }} / {{ Math.ceil(state.images.length / 8) }}</small><button class="ui-button quiet sm" :disabled="(candidatePage + 1) * 8 >= state.images.length" @click="showPage(1)">下一页</button></div><button v-if="!state.images.length" class="ui-button sm" @click="emit('go', 'images')">打开图片工作台</button>
          </section>
          <section class="flow-ai"><label>本轮故事要求<textarea v-model="state.draft.instruction" :disabled="state.loading" rows="3" placeholder="描述剧情、节奏，以及这些图片的关系" @input="workbench.changed()" /></label><button class="ui-button primary" :disabled="operationBusy || (!state.selectedImages.length && !state.draft.shots.length) || state.conflict" @click="workbench.mutate('generate')">{{ generateLabel }}</button><small>按已填内容与本轮要求整理，补全空缺并自动修正动作、运镜、时长与转场中的问题。保留有效剧情、图片版本和镜头顺序，不自动把不同人物认作同一角色。</small></section>
          <section v-if="state.candidateRunId" class="flow-notice"><p>生成结果已保留为候选，可保存为新版本，无需再次生成。</p><button class="ui-button sm" :disabled="operationBusy || state.dirty" @click="workbench.mutate('candidate')">采用已生成候选</button></section>
          <section class="flow-review" aria-label="故事校验">
            <div class="flow-col-head"><strong>故事校验</strong><button class="ui-button sm" :disabled="operationBusy || !state.draft.shots.length || state.conflict" @click="workbench.mutate('review')">{{ state.operation === 'review' ? '正在校验' : '校验当前故事' }}</button></div>
            <p v-if="workbench.dirty.value" class="pane-note">修改后需要重新校验。</p>
            <p v-else-if="!review?.validation_requested" class="pane-note">{{ review ? '初稿已完成内部检查，请点击「校验当前故事」后再确认。' : '先完成镜头内容，再校验和确认当前版本。' }}</p>
            <p v-else class="flow-feedback" :class="{ 'is-error': review.status === 'needs_revision' }">{{ review.status === 'pass' ? '校验通过' : review.status === 'warning' ? '校验通过，优化建议见页面顶部' : '需修改的问题已显示在页面顶部' }}</p>
            <a v-if="review?.validation_requested && review.findings.length" href="#story-validation-results" class="flow-validation-link">查看顶部问题与建议</a>
            <p v-if="review?.validation_requested && review.status === 'warning'" class="pane-note">确认当前故事即采用此版本。</p>
            <div class="flow-action"><button class="ui-button primary" :disabled="operationBusy || !workbench.canConfirm.value || workbench.confirmed.value" @click="workbench.mutate('confirm')">{{ state.operation === 'confirm' ? '正在确认' : workbench.confirmed.value ? '当前版本已确认' : '确认当前故事' }}</button><button class="ui-button sm" :disabled="!workbench.confirmed.value" @click="emit('go', 'export')">进入导出材料</button></div>
          </section>
        </aside>
      </div>
      <section class="flow-history"><button class="ui-button quiet sm" :aria-expanded="historyOpen" @click="historyOpen = !historyOpen">故事历史（{{ state.versions.length }}）</button><ul v-if="historyOpen" class="flow-list"><li v-for="version in state.versions" :key="version.version"><div class="flow-col-head"><strong>v{{ version.version }} · {{ version.title || '未命名故事' }}</strong><button class="ui-button sm" :disabled="operationBusy || version.version === state.view?.story?.version" @click="restoreChoice = version.version">恢复为新版本</button></div><small>{{ version.shots.length }} 个镜头 · {{ version.created_at }}</small><p v-if="restoreChoice === version.version">历史保留，恢复后需要重新校验确认。<button class="ui-button sm" :disabled="operationBusy" @click="workbench.mutate('restore', version.version); restoreChoice = null">确认恢复</button><button class="ui-button quiet sm" @click="restoreChoice = null">取消</button></p></li></ul></section>
    </template>
    <template v-else>
      <p v-if="!workbench.confirmed.value" class="flow-notice" role="status">{{ workbench.dirty.value ? '故事有未保存修改，请回到故事编排重新校验并确认。' : '请先校验并确认当前故事，再导出制作材料。' }}<button class="ui-button sm" @click="emit('go', 'story')">打开故事编排</button></p>
      <div class="flow-columns"><section class="flow-col"><strong>视频制作文字 <small v-if="state.view?.story">v{{ state.view.story.version }}</small></strong><pre v-if="state.view?.export_text" class="flow-pre" tabindex="0">{{ state.view.export_text }}</pre><p v-else class="pane-note">完成故事后将在这里显示完整剧情、动作、运镜、时长、转场和图片文件名。</p></section><section class="flow-col"><strong>镜头与图片对应</strong><ol class="flow-list"><li v-for="shot in state.view?.manifest?.shots ?? []" :key="shot.shot_id"><strong>镜头 {{ shot.sequence_number }}</strong><img v-if="exportImage(shot) && state.previews[exportImage(shot)!.artifact_id]" class="flow-export-image" :src="state.previews[exportImage(shot)!.artifact_id]" :alt="`导出镜头 ${shot.sequence_number} 的图片`" role="button" tabindex="0" @click="openImage(exportImage(shot))" @keydown.enter="openImage(exportImage(shot))" @error="workbench.previewFailed(exportImage(shot)!)" /><div v-else class="flow-image-status" role="status"><small>{{ exportImage(shot) ? state.previewErrors[exportImage(shot)!.artifact_id] || '正在读取图片' : '图片暂不可读取' }}</small><button v-if="exportImage(shot) && state.previewErrors[exportImage(shot)!.artifact_id]" class="ui-button quiet sm" @click="workbench.preview(exportImage(shot)!, true)">重新读取图片</button></div><span>{{ shot.filename }} · v{{ shot.image_version }}</span><small>{{ shot.duration_seconds }} 秒 · {{ shot.transition }}</small></li></ol><p v-if="!state.view?.manifest" class="pane-note">暂无已确认素材清单。</p><div v-for="missing in state.view?.missing_images ?? []" :key="missing.shot_id" class="flow-notice is-error" role="alert"><div><strong>{{ shotNumber(missing.shot_id) }} 图片不可导出</strong><p>{{ missing.reason }}</p><button class="ui-button sm" @click="emit('go', 'story')">替换镜头图片</button><button class="ui-button sm" @click="emit('repair', missing.source_project_id ?? '')">到图片工作台重传</button></div></div></section></div>
      <section class="flow-action"><button class="ui-button primary" :disabled="!workbench.canExport.value || operationBusy" @click="copyText"><Copy :size="15" /> 复制视频制作文字</button><button class="ui-button" :disabled="!workbench.canExport.value || operationBusy" @click="workbench.download()"><Download :size="15" /> {{ state.operation === 'export' ? '正在打包图片' : '下载图片素材包' }}</button><button class="ui-button" @click="emit('go', 'video')">进入视频生成</button><small>素材包包含故事、镜头清单和 PNG / JPEG / WebP 原始图片；下一步用同一份镜头与图片生成视频。</small></section><p v-if="copied" class="flow-feedback" role="status"><Check :size="14" /> 已复制。请在视频工具中同时上传对应图片。</p><p v-else-if="copyFailed" class="flow-error" role="alert">复制失败，请手动选中上方全文复制。</p><p v-if="state.view?.exports.length" class="pane-note">已有 {{ state.view.exports.length }} 次素材包导出记录。当前材料用于视频制作。</p>
      <div v-if="!workbench.confirmed.value" class="flow-notice" role="status">
        <template v-if="!state.view?.story"><span>先选择图片并编排作品故事。</span><button class="ui-button sm" @click="emit('go', 'story')">开始故事编排</button></template>
        <template v-else-if="!review?.validation_requested || workbench.dirty.value"><span>当前故事尚未完成校验，校验后才能确认和导出。</span><button class="ui-button sm" :disabled="operationBusy || state.conflict" @click="workbench.mutate('review')">{{ state.operation === 'review' ? '正在校验故事' : '校验当前故事' }}</button></template>
        <template v-else-if="review.status === 'needs_revision'"><span>校验发现需要修改的问题，请先处理后再次校验。</span><button class="ui-button sm" @click="emit('go', 'story')">查看并修改故事</button></template>
        <template v-else><span>{{ review.status === 'warning' ? '校验通过，有优化建议；确认即采用此版本并可导出。' : '故事校验已通过，确认此版本后可以导出。' }}</span><button class="ui-button sm" :disabled="operationBusy || !workbench.canConfirm.value" @click="workbench.mutate('confirm')">确认当前故事</button><button v-if="review.status === 'warning'" class="ui-button quiet sm" @click="emit('go', 'story')">查看建议</button></template>
      </div>
    </template>
  </section>
</template>

<style scoped>
/* 全屏预览层：Teleport 到 body 后 z-index 在根层叠上下文里生效，
   不再被 .stage-scroll 裁剪，也不再被工作台页眉/输入区盖住。 */
.flow-large-image { position:fixed; inset:0; z-index:200; display:grid; grid-template-rows:auto minmax(0,1fr) auto; gap:var(--space-4); padding:var(--space-4) var(--space-6) var(--space-3); background:var(--surface); }
.flow-large-bar { display:flex; align-items:center; justify-content:space-between; gap:var(--space-4); padding-bottom:var(--space-3); border-bottom:1px solid var(--border-muted); }
.flow-large-bar small { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--text-secondary); font-size:var(--font-meta); }
.flow-large-close { display:inline-flex; align-items:center; gap:var(--space-2); flex-shrink:0; }
.flow-large-key { padding:0 var(--space-1); border:1px solid var(--border-strong); border-radius:var(--radius-tile); color:var(--text-muted); font-size:var(--font-kicker); line-height:1.5; }
.flow-large-canvas { min-height:0; }
.flow-large-hint { margin:0; text-align:center; color:var(--text-muted); font-size:var(--font-meta); }
.flow-stage { display:grid; gap:var(--space-6); }
.flow-stage-head { display:flex; align-items:flex-start; justify-content:space-between; gap:var(--space-4); padding-bottom:var(--space-4); border-bottom:1px solid var(--border-muted); }
.flow-stage-head h3 { margin:0; font-size:var(--font-section); font-weight:var(--weight-medium); }
.flow-stage-head small,.flow-save,.flow-col-head>span { color:var(--text-secondary); font-size:var(--font-meta); line-height:1.6; }
.flow-stage-head small { display:block; margin-top:var(--space-2); }.flow-save { flex-shrink:0; }
.flow-columns { display:grid; grid-template-columns:minmax(0,1.65fr) minmax(280px,1fr); gap:var(--space-6); align-items:start; }
.flow-col,.flow-side,.flow-editor { display:grid; gap:var(--space-4); align-content:start; min-width:0; }.flow-side { background:var(--surface-subtle); padding:var(--space-5); border:1px solid var(--border-muted); border-radius:var(--radius-sm); }
.flow-col-head { display:flex; align-items:center; justify-content:space-between; gap:var(--space-2); }.flow-col-head strong,.flow-col>strong { font-size:var(--font-control); font-weight:var(--weight-medium); }
.flow-editor label,.flow-ai label,.flow-shot-image label { display:grid; gap:var(--space-2); color:var(--text-secondary); font-size:var(--font-meta); }
.flow-stage input:not([type=checkbox]),.flow-stage textarea,.flow-stage select { width:100%; box-sizing:border-box; padding:var(--space-2) var(--space-3); border:1px solid var(--border-strong); border-radius:var(--radius-control); background:var(--surface); color:var(--text-primary); font:var(--font-control)/1.6 var(--font-ui); min-width:0; }
.flow-stage textarea { resize:vertical; }.flow-stage input:focus-visible,.flow-stage textarea:focus-visible,.flow-stage select:focus-visible { outline:var(--focus-ring-width) solid var(--focus-ring-color); outline-offset:1px; }
.flow-shots { display:grid; gap:var(--space-4); margin:0; padding:0; list-style:none; }.flow-shots>li { border:1px solid var(--border-muted); border-radius:var(--radius-sm); padding:var(--space-4); background:var(--surface); }
.flow-shots header { display:flex; align-items:center; gap:var(--space-2); margin-bottom:var(--space-3); }.flow-shots header strong { font-size:var(--font-control); }.flow-shot-actions { display:flex; gap:var(--space-1); margin-left:auto; }.flow-grip { cursor:grab; }
.flow-shot-content { display:grid; grid-template-columns:180px minmax(0,1fr); gap:var(--space-4); }.flow-shot-image { display:grid; gap:var(--space-2); align-content:start; min-width:0; }.flow-shot-image img { width:100%; height:auto; aspect-ratio:16/9; object-fit:contain; background:var(--canvas-inset); border-radius:var(--radius-sm); }.flow-shot-image small { overflow-wrap:anywhere; color:var(--text-secondary); font-size:var(--font-meta); }
.flow-shot-fields { display:grid; gap:var(--space-3); }.flow-field-pair { display:grid; grid-template-columns:100px minmax(0,1fr); gap:var(--space-3); }.flow-images { list-style:none; margin:var(--space-3) 0; padding:0; display:grid; gap:var(--space-3); }.flow-images li { display:grid; gap:var(--space-2); padding-bottom:var(--space-3); border-bottom:1px solid var(--border-muted); }.flow-images li>button { justify-self:end; }.flow-image-pick { display:flex; align-items:center; gap:var(--space-2); min-width:0; cursor:pointer; }.flow-image-pick img,.flow-thumb-missing { width:52px; height:52px; object-fit:contain; background:var(--canvas-inset); flex-shrink:0; }.flow-thumb-missing { font-size:var(--font-meta); display:grid; place-items:center; }.flow-image-pick span { min-width:0; }.flow-image-pick strong { display:block; font-size:var(--font-meta); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.flow-image-pick small { display:block; color:var(--text-muted); font-size:var(--font-meta); overflow-wrap:anywhere; }
.flow-ai,.flow-review { display:grid; gap:var(--space-3); padding-top:var(--space-4); border-top:1px solid var(--border-muted); }.flow-ai small,.flow-action small { color:var(--text-secondary); font-size:var(--font-meta); line-height:1.6; }.flow-checkbox { display:flex; align-items:start; gap:var(--space-2); font-size:var(--font-control); }.flow-page,.flow-action { display:flex; flex-wrap:wrap; align-items:center; gap:var(--space-3); }.flow-page { justify-content:space-between; }
.flow-list { list-style:none; display:grid; gap:var(--space-3); margin:0; padding:0; }.flow-list li { display:grid; gap:var(--space-1); padding:var(--space-3) 0; border-bottom:1px solid var(--border-muted); font-size:var(--font-control); }.flow-list small { color:var(--text-secondary); font-size:var(--font-meta); }
/* 校验结果集中页头：先读位置和修改建议，历史长说明按需展开。 */
.flow-validation-body { flex:1; min-width:0; }.flow-validation-list li { grid-template-columns:160px minmax(0,1fr); gap:var(--space-3); }.flow-validation-list strong small { display:block; font-weight:var(--weight-regular); }.flow-validation-list details { font-size:var(--font-meta); }.flow-validation-list summary { cursor:pointer; color:var(--text-secondary); }.flow-validation-list p { overflow-wrap:anywhere; }
.flow-validation-link { color:var(--text-secondary); font-size:var(--font-meta); }.flow-validation { scroll-margin-top:var(--space-6); }
/* 展开按钮贴在校验块左对齐，避免和 .flow-notice 的图标列挤在一起。 */
.flow-validation-more { align-self:flex-start; margin-top:var(--space-1); }
.flow-fix { color:var(--text-primary); font-weight:var(--weight-medium); }.flow-notice { display:flex; align-items:start; flex-wrap:wrap; gap:var(--space-2); margin:0; padding:var(--space-3); border-left:3px solid var(--warning-border); background:var(--warning-soft); color:var(--text-secondary); font-size:var(--font-control); line-height:1.6; }.flow-notice p { margin:0 0 var(--space-2); }.flow-notice.is-error { border-color:var(--danger-border); background:var(--danger-soft); }.flow-error,.flow-feedback.is-error { color:var(--danger); }.flow-feedback { display:flex; align-items:center; gap:var(--space-2); color:var(--success); margin:0; font-size:var(--font-control); }.flow-pre { white-space:pre-wrap; overflow-wrap:anywhere; overflow:auto; max-height:65vh; margin:0; padding:var(--space-4); border:1px solid var(--border-muted); border-radius:var(--radius-sm); background:var(--surface-subtle); color:var(--text-primary); font:var(--font-control)/1.8 var(--font-ui); }.flow-export-image { width:100%; height:auto; max-height:240px; object-fit:contain; background:var(--canvas-inset); border-radius:var(--radius-sm); }.flow-image-status { display:grid; place-content:center; gap:var(--space-2); min-height:80px; background:var(--surface-subtle); padding:var(--space-3); }.flow-history { border-top:1px solid var(--border-muted); padding-top:var(--space-3); }
@media(max-width:1100px) { .flow-columns { grid-template-columns:1fr; }.flow-side { padding:var(--space-4); } }
@media(max-width:600px) { .flow-stage-head { flex-wrap:wrap; }.flow-shot-content { grid-template-columns:1fr; }.flow-shot-image { grid-template-columns:100px minmax(0,1fr); }.flow-shot-image img { grid-row:span 3; }.flow-col-head { flex-wrap:wrap; }.flow-validation-list li { grid-template-columns:1fr; } }
</style>
