<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowLeft, Check, Columns2, Copy, Download, Hand, ImageOff, ImagePlus, Maximize, Minus, MousePointer2, Plus, Sparkles, Trash2, Undo2, X } from 'lucide-vue-next'
import ComicImageImport from './ComicImageImport.vue'
import ImageViewport from '../ui/ImageViewport.vue'
import { fittedImage, boundImagePan, zoomImage } from '../ui/imageViewport'
import { finalImagePrompt } from '../../domains/comic/shotGeneration'
import {
  analyzeComicImage, compareComicImages, confirmComicEditInstruction, createComicEditInstruction,
  deleteComicImage,
  getArtifactContentUrl, getComicImageReport, getComicImageReports, getComicImages, getComicImageVersions,
  getComicProject, getComicImageRequests, getComicReviewDraft, getComicWorkbench, importComicImage, restoreComicImageFile,
  saveComicReviewDraft, saveComicWorkbench, selectComicImage,
  type ComicComparison, type ComicEditInstruction, type ComicEditRegion, type ComicEditScope,
  type ComicEditDiagnostics, type ComicImageAnalysis, type ComicImageRef, type ComicImageView, type ComicInstructionTarget, type ComicMediaWrite, type ComicReviewDraft,
} from '../../services/core'

const props = defineProps<{ projectId: string; conversationId: string; task: string; legacyImages: ComicImageView[]; briefVersion?: number; initialView?: string; newImport?: ComicImageRef }>()
const emit = defineEmits<{ changed: []; importConsumed: [image: ComicImageRef]; state: [value: { projectId: string; conversationId: string; hasImages: boolean; adopted: boolean }] }>()
// Each mounted workbench belongs to one project/conversation; parent keys enforce isolation.
const owner = props.conversationId, projectId = props.projectId
const families = ref<ComicImageView[]>([]), versions = ref<ComicImageView[]>([])
const current = ref<ComicImageView | null>(null), base = ref<ComicImageView | null>(null)
const analysis = ref<ComicImageAnalysis | null>(null), instructions = ref<ComicEditInstruction[]>([])
const reportCursor = ref<string | null>(null), nextVersion = ref<number | null>(null)
const draft = ref<ComicReviewDraft | null>(null), draftVersion = ref(0), workspaceVersion = ref(0)
const remoteDraft = ref<{ version: number; state: ComicReviewDraft | null } | null>(null)
const autoAnalyze = ref(true), loading = ref(true), operation = ref(''), error = ref(''), feedback = ref('')
const operationErrors = ref<Record<string, string>>({}), organizing = ref(false)
const saveState = ref(''), saveError = ref(''), historyOpen = ref(false), instructionHistory = ref(false)
const historyInstruction = ref<ComicEditInstruction | null>(null), regions = ref<ComicEditRegion[]>([])
const wholeFindingIds = ref<string[]>([]), instructionTarget = ref<ComicInstructionTarget>({ mode: 'selected_regions' })
const scopeDirty = ref(false), expandedItem = ref(''), drawingFinding = ref('')
const panelTab = ref<'requirements' | 'analysis'>('requirements'), editDiagnostics = ref<ComicEditDiagnostics | null>(null)
const diagnosticsOpen = ref(false)
const workbenchElement = ref<HTMLElement | null>(null), canvasElement = ref<HTMLElement | null>(null)
const panelElement = ref<HTMLElement | null>(null)
const canvasSize = ref({ width: 0, height: 0 }), panelHeight = ref(0), workbenchHeight = ref(0)
const imageSizes = ref<Record<string, { width: number; height: number }>>({})
const zoom = ref(1), pan = ref({ x: 0, y: 0 }), viewTool = ref<'select' | 'pan'>('select'), versionsExpanded = ref(false)
const compact = computed(() => workbenchHeight.value > 0 && workbenchHeight.value < 600)
const shortPanel = computed(() => compact.value || (panelHeight.value > 0 && panelHeight.value < 480))
const currentSize = computed(() => imageSizes.value[current.value?.artifact_id ?? ''])
const viewportSize = computed(() => fittedImage(currentSize.value, canvasSize.value))
const viewportStyle = computed(() => ({ width: viewportSize.value.width + 'px', height: viewportSize.value.height + 'px',
  left: (canvasSize.value.width - viewportSize.value.width) / 2 + 'px', top: (canvasSize.value.height - viewportSize.value.height) / 2 + 'px' }))
const fitScale = computed(() => currentSize.value?.width ? viewportSize.value.width / currentSize.value.width : 0)
const displaySize = computed(() => ({ width: (currentSize.value?.width ?? 0) * fitScale.value * zoom.value,
  height: (currentSize.value?.height ?? 0) * fitScale.value * zoom.value }))
const imageStyle = computed(() => ({ width: displaySize.value.width + 'px', height: displaySize.value.height + 'px',
  left: ((viewportSize.value.width - displaySize.value.width) / 2 + pan.value.x) + 'px',
  top: ((viewportSize.value.height - displaySize.value.height) / 2 + pan.value.y) + 'px' }))
function boundPan(): void { pan.value = boundImagePan(pan.value, viewportSize.value, zoom.value) }
let resizeObserver: ResizeObserver | null = null
function measureView(): void {
  const priorScale = fitScale.value
  const rect = canvasElement.value?.getBoundingClientRect?.()
  if (rect) canvasSize.value = { width: rect.width, height: rect.height }
  panelHeight.value = panelElement.value?.getBoundingClientRect?.().height ?? 0
  workbenchHeight.value = workbenchElement.value?.getBoundingClientRect?.().height ?? 0
  if (priorScale && fitScale.value) {
    const ratio = fitScale.value / priorScale
    pan.value = { x: pan.value.x * ratio, y: pan.value.y * ratio }
  }
  boundPan()
}
watch([canvasElement, panelElement, workbenchElement], () => {
  resizeObserver?.disconnect()
  if (typeof ResizeObserver !== 'undefined') {
    resizeObserver ??= new ResizeObserver(measureView)
    for (const element of [canvasElement.value, panelElement.value, workbenchElement.value]) if (element) resizeObserver.observe(element)
    measureView()
  }
}, { flush: 'post' })
function fitView(): void { zoom.value = 1; pan.value = { x: 0, y: 0 }; viewTool.value = 'select' }
// 点百分比只把缩放拉回 100%，不改变当前工具（与"适应画布"区分开）。
function resetZoom(): void { zoom.value = 1; pan.value = { x: 0, y: 0 } }
watch(() => current.value?.artifact_id, fitView)
function changeZoom(multiplier: number, anchor = { x: 0, y: 0 }): void {
  const view = zoomImage(pan.value, zoom.value, multiplier, anchor, viewportSize.value)
  zoom.value = view.zoom; pan.value = view.pan
}
function wheelZoom(event: WheelEvent): void {
  if (drag || panDrag || !currentSize.value || !current.value || !ready.value[current.value.artifact_id]) return
  const rect = canvasElement.value?.getBoundingClientRect()
  if (!rect) return
  const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.height : 1)
  changeZoom(Math.exp(-delta * .0015), { x: event.clientX - rect.left - rect.width / 2,
    y: event.clientY - rect.top - rect.height / 2 })
}
const panning = ref(false)
let panDrag: { pointer: number; x: number; y: number; start: { x: number; y: number } } | null = null
function startPan(event: PointerEvent): void {
  if (drag || !currentSize.value || (event.button !== 0 && event.button !== 1) || (drawing.value && event.button !== 1)) return
  panDrag = { pointer: event.pointerId, x: event.clientX, y: event.clientY, start: { ...pan.value } }
  panning.value = true
  canvasElement.value?.setPointerCapture(event.pointerId); event.preventDefault()
}
function movePan(event: PointerEvent): void {
  if (panDrag?.pointer !== event.pointerId) return
  pan.value = { x: panDrag.start.x + event.clientX - panDrag.x, y: panDrag.start.y + event.clientY - panDrag.y }
  boundPan()
}
function endPan(event: PointerEvent): void {
  if (panDrag?.pointer !== event.pointerId) return
  panDrag = null; panning.value = false; canvasElement.value?.releasePointerCapture(event.pointerId)
}
type RemovedSelection = { round: string; regions: ComicEditRegion[]; findingId: string | null; selected: boolean; whole: boolean; dismissed: boolean }
const removedSelection = ref<RemovedSelection | null>(null)
const requirementElements = new Map<string, HTMLElement>()
const focus = ref(''), drawing = ref(false), comparison = ref(false), comparisonKey = ref('')
const comparisonReport = ref<ComicComparison | null>(null), importFile = ref<File | null>(null)
const importMode = ref<'new' | 'version'>('new'), importParent = ref(''), associateInstruction = ref(false), importInstructionId = ref('')
const importInstructions = ref<ComicEditInstruction[]>([]), importCursor = ref<string | null>(null)
const autoAnalysisStarted = new Set<string>()
let automaticAnalysisImage: ComicImageRef | null = null
let pendingImport: { file: File; fingerprint: string; body: Parameters<typeof importComicImage>[2] } | null = null
const boundsElement = ref<HTMLElement | null>(null), restoring = ref(false)
// 删除整张图片（含全部版本）：先确认再执行，成功后重新读取工作台，让基础图与草稿自然回退。
const deleteTarget = ref<ComicImageView | null>(null)
const urls = ref<Record<string, string>>({}), previewErrors = ref<Record<string, string>>({})
const ready = ref<Record<string, boolean>>({})
const previewPending = new Map<string, Promise<void>>(), previewAccess = new Map<string, number>()
let disposed = false, selectionEpoch = 0, initializeSequence = 0, saveTimer: ReturnType<typeof setTimeout> | undefined
let saving: Promise<boolean> | null = null, preferenceQueue: Promise<void> = Promise.resolve()
let pendingSave: { body: ComicReviewDraft; version: number; id: string } | null = null
const localUnavailableDrafts = new Map<string, { state: ComicReviewDraft; version: number }>()
type EditRequest = Parameters<typeof createComicEditInstruction>[1]
const pendingEdit = ref<{ request: EditRequest; snapshot: string; uncertain: boolean } | null>(null)
const checkingRequest = ref(false)
const key = (image: ComicImageRef) => image.image_id + ':' + image.version
const imageRef = (image: ComicImageRef): ComicImageRef => ({ image_id: image.image_id, version: image.version })
const same = (left: ComicImageRef | null, right: ComicImageRef | null) => !!left && !!right && key(left) === key(right)
const onBase = computed(() => same(current.value, base.value))
const allVersions = computed(() => [...versions.value].sort((a, b) => b.version - a.version))
const strip = computed(() => historyOpen.value ? allVersions.value : allVersions.value.slice(0, 5))
const selectedFamily = computed(() => families.value.find(item => item.image_id === current.value?.image_id))
const adopted = computed(() => selectedFamily.value?.selection?.selected_version)
const unavailable = (image: ComicImageView | null) => image?.available === false
const unavailableReason = (image: ComicImageView | null) => image?.unavailable_reason || '图片文件不可用，请选择有效版本或导入新图片。'
const recoveryHint = (image: ComicImageView | null) => image?.lifecycle_state === 'trashed'
  ? '图片已在回收站，可到资产回收站恢复后重新读取。'
  : image?.lifecycle_state === 'purged' ? '图片已彻底删除，请导入新素材并重新选择采用版本。'
    : '可重传内容完全相同的原文件，或选择有效版本；不同图片请作为新素材导入。'
const visibleInstruction = computed(() => historyInstruction.value ?? instructions.value.find(item => item.artifact_id === draft.value?.instruction_artifact_id) ?? null)
const otherImage = computed(() => versions.value.find(item => key(item) === comparisonKey.value) ?? null)
const targetParent = computed(() => versions.value.find(item => key(item) === importParent.value) ?? null)
const parentInstructions = computed(() => [...new Map([...instructions.value, ...importInstructions.value].map(item => [item.artifact_id, item])).values()].filter(item =>
  !item.requires_confirmation && targetParent.value && same(item.basis.source_image, targetParent.value)))
const parentInstruction = computed(() => parentInstructions.value.find(item => item.artifact_id === importInstructionId.value))
const previewUrl = (image: ComicImageView | null) => image ? urls.value[image.artifact_id] ?? '' : ''
const candidateRegions = computed(() => (analysis.value?.output.findings ?? []).flatMap((item, index) => {
  if (!item.region || draft.value?.dismissed_finding_region_ids?.includes(item.finding_id)
      || wholeFindingIds.value.includes(item.finding_id) || regions.value.some(region => region.finding_id === item.finding_id)) return []
  return [{ id: 'finding:' + item.finding_id, number: index + 1, bounds: item.region, selected: false }]
}))
const overlays = computed(() => onBase.value ? [...candidateRegions.value,
  ...regions.value.map((item, index) => ({ id: item.region_id,
    number: item.finding_id ? (analysis.value?.output.findings.findIndex(f => f.finding_id === item.finding_id) ?? -1) + 1 : item.label || '手动' + (index + 1),
    bounds: item.bounds, selected: true }))] : [])
const modifications = computed(() => [
  ...(draft.value?.selected_finding_ids ?? []).map(id => {
    const finding = analysis.value?.output.findings.find(item => item.finding_id === id)
    const linked = regions.value.filter(item => item.finding_id === id)
    return { id: 'finding:' + id, findingId: id, finding, regions: linked,
      title: `${(analysis.value?.output.findings.findIndex(item => item.finding_id === id) ?? -1) + 1}. ${finding?.observation ?? '建议已失效'}`,
      status: wholeFindingIds.value.includes(id) ? '整图' : linked.length ? '区域' : '待定位' }
  }),
  ...regions.value.filter(item => !item.finding_id).map(item => ({ id: item.region_id, findingId: null, finding: undefined,
    regions: [item], title: item.label || '人工选区', status: '区域' })),
])
const pendingModifications = computed(() => modifications.value.filter(item => item.status === '待定位' || (item.findingId && !item.finding)))
const activeModification = computed(() => modifications.value.find(item => item.id === expandedItem.value))
const scopeSummary = computed(() => {
  const extra = draft.value?.instruction.trim() ? 1 : 0
  return `已选 ${modifications.value.length + extra} 条 · 区域 ${modifications.value.filter(item => item.status === '区域').length + (extra && !invalidSupplement.value && instructionTarget.value.mode !== 'whole' ? 1 : 0)} 条 · 整图 ${modifications.value.filter(item => item.status === '整图').length + (extra && instructionTarget.value.mode === 'whole' ? 1 : 0)} 条 · 待定位 ${pendingModifications.value.length + (invalidSupplement.value ? 1 : 0)} 条`
})
const supplementTargetValue = computed(() => instructionTarget.value.mode === 'region' ? 'region:' + instructionTarget.value.region_id : instructionTarget.value.mode)
const invalidSupplement = computed(() => !!draft.value?.instruction.trim() && (
  instructionTarget.value.mode === 'selected_regions' ? !regions.value.length
    : instructionTarget.value.mode === 'region' ? !regions.value.some(item => item.region_id === instructionTarget.value.region_id) : false))
function regionalScope(): ComicEditScope {
  const global = wholeFindingIds.value.length > 0 || (!!draft.value?.instruction.trim() && instructionTarget.value.mode === 'whole')
  const local = regions.value.length > 0 || pendingModifications.value.length > 0
  return { mode: global ? local ? 'mixed' : 'whole' : 'regions',
    regions: regions.value.map(item => ({ ...item, bounds: [...item.bounds] })),
    whole_finding_ids: [...wholeFindingIds.value], instruction_target: { ...instructionTarget.value } }
}
function snapshot(): ComicReviewDraft | null {
  return draft.value ? { ...draft.value, image: { ...draft.value.image }, selected_finding_ids: [...draft.value.selected_finding_ids],
    ...(draft.value.dismissed_finding_region_ids ? { dismissed_finding_region_ids: [...draft.value.dismissed_finding_region_ids] } : {}),
    edit_scope: scopeDirty.value ? regionalScope() : draft.value.edit_scope } : null
}
function fail(value: unknown): string {
  if (value instanceof TypeError && /fetch|network|load failed/i.test(value.message)) return '连接中断，请重新读取；当前内容已保留。'
  return value instanceof Error ? value.message : String(value)
}
const stableScope = (scope: unknown): string => JSON.stringify(scope, (_key, value) => value && typeof value === 'object' && !Array.isArray(value)
  ? Object.fromEntries(Object.entries(value).sort(([left], [right]) => left.localeCompare(right))) : value)
function fresh(image: ComicImageView): ComicReviewDraft {
  return { image: imageRef(image), round_id: crypto.randomUUID(), task: props.task || '检查当前图片',
    analysis_artifact_id: null, selected_finding_ids: [], edit_scope: null, instruction: '',
    instruction_artifact_id: null, phase: 'editing', dismissed_finding_region_ids: [] }
}
async function preview(image: ComicImageView, retry = false): Promise<void> {
  const id = image.artifact_id
  if (unavailable(image)) {
    if (urls.value[id]) URL.revokeObjectURL(urls.value[id])
    delete urls.value[id]; delete ready.value[id]
    previewErrors.value[id] = unavailableReason(image)
    return
  }
  previewAccess.set(id, Date.now())
  if (urls.value[id] && !retry) return
  if (previewPending.has(id)) return previewPending.get(id)
  if (retry && urls.value[image.artifact_id]) URL.revokeObjectURL(urls.value[image.artifact_id])
  delete urls.value[image.artifact_id]; delete previewErrors.value[image.artifact_id]; delete ready.value[image.artifact_id]
  const pending = (async () => {
    try {
      const url = await getArtifactContentUrl(id, true)
      if (url && !disposed && !unavailable(image)) { urls.value[id] = url; prunePreviews() }
      else if (url) URL.revokeObjectURL(url)
    } catch (failure) { if (!disposed) previewErrors.value[id] = fail(failure) }
  })()
  previewPending.set(id, pending)
  try { await pending } finally { previewPending.delete(id) }
}
function prunePreviews(): void {
  const pinned = new Set([current.value?.artifact_id, base.value?.artifact_id, otherImage.value?.artifact_id,
    ...allVersions.value.slice(0, 5).map(item => item.artifact_id)])
  const ids = Object.keys(urls.value).sort((a, b) => (previewAccess.get(a) ?? 0) - (previewAccess.get(b) ?? 0))
  for (const id of ids) {
    if (Object.keys(urls.value).length <= 12) break
    if (pinned.has(id)) continue
    URL.revokeObjectURL(urls.value[id]!); delete urls.value[id]; delete ready.value[id]; previewAccess.delete(id)
  }
}
function decodeError(image: ComicImageView): void {
  previewErrors.value[image.artifact_id] = '图片内容无法显示，请重试读取或核对原文件。'
  ready.value[image.artifact_id] = false
}
async function saveDraft(): Promise<boolean> {
  clearTimeout(saveTimer)
  if (drag) { saveState.value = '正在调整选区，结束后保存'; return false }
  if (saving) { const ok = await saving; if (!ok) return false; return saveDraft() }
  const body = snapshot()
  if (!body) return true
  if (unavailable(base.value)) {
    localUnavailableDrafts.set(key(body.image), { state: body, version: draftVersion.value })
    saveState.value = '原图不可用，修改仅保留在本页'
    return true
  }
  const identity = key(body.image), encoded = JSON.stringify(body)
  if (!pendingSave || JSON.stringify(pendingSave.body) !== encoded) pendingSave = { body, version: draftVersion.value, id: crypto.randomUUID() }
  const pending = pendingSave
  saveState.value = '正在保存'; saveError.value = ''
  saving = (async () => {
    try {
      const result = await saveComicReviewDraft(owner, pending.version, pending.body, pending.id)
      if (!disposed && draft.value && key(draft.value.image) === identity) {
        draftVersion.value = result.version
        if (pendingSave === pending) pendingSave = null
        saveState.value = JSON.stringify(snapshot()) === encoded ? '已保存' : '尚有修改待保存'
      }
      return true
    } catch (failure) { if (!disposed) { saveError.value = fail(failure); saveState.value = '保存失败，内容仍在本页' }; return false }
  })()
  const ok = await saving
  saving = null
  if (ok && !disposed && JSON.stringify(snapshot()) !== encoded) return saveDraft()
  return ok
}
function edit(save = true): void {
  if (!draft.value) return
  if (error.value === operationErrors.value.edit) error.value = ''
  operationErrors.value.edit = ''
  editDiagnostics.value = null
  diagnosticsOpen.value = false
  if (!scopeDirty.value && draft.value.edit_scope && !draft.value.edit_scope.instruction_target) draft.value.round_id = crypto.randomUUID()
  scopeDirty.value = true
  draft.value.phase = 'editing'; draft.value.instruction_artifact_id = null
  historyInstruction.value = null; feedback.value = ''; saveState.value = '待保存'
  if (save) { clearTimeout(saveTimer); saveTimer = setTimeout(() => void saveDraft(), 700) }
}
async function reloadDraft(): Promise<void> {
  if (!base.value) return
  try { remoteDraft.value = await getComicReviewDraft(base.value, owner) }
  catch (failure) { saveError.value = fail(failure) }
}
function useRemote(): void {
  if (!remoteDraft.value || !base.value) return
  draftVersion.value = remoteDraft.value.version
  draft.value = applyDraft(remoteDraft.value.state ?? fresh(base.value)); remoteDraft.value = null
  pendingSave = null; saveError.value = ''; saveState.value = '已读取服务端草稿'
}
async function keepLocal(): Promise<void> {
  if (!remoteDraft.value) return
  draftVersion.value = remoteDraft.value.version; remoteDraft.value = null; pendingSave = null
  await saveDraft()
}
function applyDraft(value: ComicReviewDraft): ComicReviewDraft {
  scopeDirty.value = false
  wholeFindingIds.value = [...(value.edit_scope?.whole_finding_ids ?? (value.edit_scope?.mode === 'whole' ? value.selected_finding_ids : []))]
  instructionTarget.value = value.edit_scope?.instruction_target ? { ...value.edit_scope.instruction_target }
    : { mode: value.edit_scope?.mode === 'whole' ? 'whole' : 'selected_regions' }
  regions.value = value.edit_scope?.regions.map(item => ({ ...item, bounds: [...item.bounds] })) ?? []
  focus.value = ''; drawing.value = false; drawingFinding.value = ''; expandedItem.value = ''; removedSelection.value = null; historyInstruction.value = null
  diagnosticsOpen.value = false; editDiagnostics.value = null; panelTab.value = 'requirements'
  return value
}
function toggleAutoAnalyze(): void { autoAnalyze.value = !autoAnalyze.value; void savePreference() }
function savePreference(): Promise<void> {
  if (unavailable(current.value) || unavailable(base.value)) return Promise.resolve()
  const state = { image: current.value ? imageRef(current.value) : null,
    edit_image: base.value ? imageRef(base.value) : null, auto_analyze: autoAnalyze.value }
  preferenceQueue = preferenceQueue.then(async () => {
    try { const result = await saveComicWorkbench(projectId, owner, workspaceVersion.value, state); workspaceVersion.value = result.version }
    catch (failure) { if (!disposed) error.value = '工作台偏好保存失败：' + fail(failure) }
  })
  return preferenceQueue
}
async function loadVersions(imageId: string, more = false): Promise<void> {
  const page = await getComicImageVersions(imageId, owner, more ? nextVersion.value ?? undefined : undefined)
  if (!page || disposed || current.value?.image_id !== imageId) return
  const merged = new Map((more ? versions.value : []).map(item => [key(item), item]))
  page.versions.forEach(item => merged.set(key(item), item)); versions.value = [...merged.values()]
  nextVersion.value = page.next_before
  const family = families.value.find(item => item.image_id === imageId)
  if (family) family.selection = page.selection
  await Promise.all(versions.value.slice(0, 5).map(item => preview(item)))
}
async function resolveVersion(ref: ComicImageRef): Promise<ComicImageView | null> {
  let found = versions.value.find(item => same(item, ref))
  if (!found) {
    const page = await getComicImageVersions(ref.image_id, owner, ref.version + 1)
    found = page?.versions.find(item => same(item, ref))
    if (found && !disposed && ref.image_id === current.value?.image_id)
      versions.value = [...versions.value.filter(item => !same(item, ref)), found]
  }
  return found ?? null
}
/* 版本号在"删除后重新导入"时会被复用，只有内容指纹能证明这份报告算的就是这张图：
   指纹不一致时宁可当作没有报告，也不把另一张图的建议显示在当前图上。 */
function fromThisContent(report: ComicImageAnalysis | ComicEditInstruction, image: ComicImageView): boolean {
  const first = report.basis?.images?.[0]
  // 只在两边都有指纹且确实不同时才拒绝：缺失指纹不代表可以判错。
  if (!first?.sha256 || !image.sha256) return true
  return first.sha256 === image.sha256
}
async function makeBase(image: ComicImageView, newRound = false, retainView = false): Promise<void> {
  if (draft.value && !(await saveDraft())) return
  const previous = same(base.value, image) && draft.value ? { state: snapshot()!, version: draftVersion.value } : null
  const epoch = ++selectionEpoch
  draft.value = null; regions.value = []; saveError.value = ''; saveState.value = ''
  base.value = image
  if (!retainView) current.value = image
  analysis.value = null; instructions.value = []
  operationErrors.value = {}
  comparison.value = false; comparisonReport.value = null; pendingSave = null; remoteDraft.value = null
  const [savedResult, reportsResult, editsResult] = await Promise.allSettled([
    getComicReviewDraft(image, owner),
    unavailable(image) ? null : getComicImageReports<ComicImageAnalysis>(projectId, owner, 'image-analyses', image),
    unavailable(image) ? null : getComicImageReports<ComicEditInstruction>(projectId, owner, 'edit-instructions', image),
    preview(image),
  ])
  if (disposed || epoch !== selectionEpoch) return
  const saved = localUnavailableDrafts.get(key(image)) ?? (savedResult.status === 'fulfilled' ? savedResult.value : null)
  if (!saved) {
    if (previous) { draftVersion.value = previous.version; draft.value = applyDraft(previous.state) }
    error.value = savedResult.status === 'rejected' ? fail(savedResult.reason) : '修改需求暂不可读取，请重新读取工作台。'
    return
  }
  const reports = reportsResult.status === 'fulfilled' ? reportsResult.value : null
  const edits = editsResult.status === 'fulfilled' ? editsResult.value : null
  draftVersion.value = saved.version
  draft.value = applyDraft(newRound ? fresh(image) : saved.state ?? fresh(image))
  instructions.value = (edits?.items ?? []).filter(item => fromThisContent(item, image))
  reportCursor.value = edits?.next_cursor ?? null
  if (unavailable(image)) {
    saveState.value = '已保留修改需求；原图不可用'
    return
  }
  async function historicalReport<T>(kind: 'image-analyses' | 'edit-instructions', id: string): Promise<T | null> {
    try { return await getComicImageReport<T>(projectId, owner, kind, id) }
    catch (failure) {
      if (!disposed && epoch === selectionEpoch) operationErrors.value.history = '部分历史报告不可用，修改需求已保留：' + fail(failure)
      return null
    }
  }
  if ([reportsResult, editsResult].some(result => result.status === 'rejected') || reports?.unavailable_items?.length || edits?.unavailable_items?.length)
    operationErrors.value.history = '部分历史报告不可用，修改需求已保留，可重新分析当前图片。'
  if (draft.value?.analysis_artifact_id) {
    analysis.value = reports?.items.find(item => item.artifact_id === draft.value?.analysis_artifact_id
      && fromThisContent(item, image)) ?? null
    if (!analysis.value) {
      const fetched = await historicalReport<ComicImageAnalysis>('image-analyses', draft.value.analysis_artifact_id)
      if (disposed || epoch !== selectionEpoch) return
      analysis.value = fetched && fromThisContent(fetched, image) ? fetched : null
    }
    if (!analysis.value && draft.value && !operationErrors.value.history) {
      // 旧版本号的分析不属于这张图：清掉指针并落盘，避免每次打开都加载错报告。
      draft.value.analysis_artifact_id = null
      await saveDraft()
    }
  }
  else if (!saved.state || newRound) {
    analysis.value = reports?.items.find(item => item.basis.task_context.task === draft.value?.task
      && fromThisContent(item, image)) ?? null
    if (analysis.value && draft.value) draft.value.analysis_artifact_id = analysis.value.artifact_id
  }
  if (draft.value?.instruction_artifact_id && !instructions.value.some(item => item.artifact_id === draft.value?.instruction_artifact_id)) {
    const report = await historicalReport<ComicEditInstruction>('edit-instructions', draft.value.instruction_artifact_id)
    if (report && fromThisContent(report, image) && !disposed && epoch === selectionEpoch) instructions.value.unshift(report)
  }
  if (disposed || epoch !== selectionEpoch) return
  saveState.value = saved.state && !newRound ? '已恢复草稿' : ''
  if (!unavailable(current.value)) await savePreference()
  if (!newRound && draft.value.phase === 'editing') await checkEditRequest(true)
}
async function initialize(): Promise<void> {
  const sequence = ++initializeSequence
  selectionEpoch++
  loading.value = true; error.value = ''
  try {
    const [items, state] = await Promise.all([getComicImages(projectId, owner, props.briefVersion), getComicWorkbench(projectId, owner)])
    if (disposed || sequence !== initializeSequence) return
    // The API already projects valid old Shot uploads. Cached Prompt links cannot revive deleted media.
    families.value = items.map(item => ({ ...item,
      label: props.legacyImages.find(legacy => legacy.image_id === item.image_id)?.label || item.label }))
    await Promise.all(items.filter(unavailable).map(item => preview(item)))
    autoAnalyze.value = state?.state?.auto_analyze ?? true; workspaceVersion.value = state?.version ?? 0
    const imported = props.newImport
    const chosen = families.value.find(item => item.image_id === imported?.image_id)
      ?? families.value.find(item => item.image_id === state?.state?.image?.image_id) ?? families.value.at(-1)
    if (chosen) {
      current.value = chosen; await loadVersions(chosen.image_id)
      const wanted = imported ?? state?.state?.image
      const source = wanted && wanted.image_id === chosen.image_id ? await resolveVersion(wanted) ?? chosen : chosen
      current.value = source
      const editRef = imported ? null : state?.state?.edit_image
      const editImage = editRef && families.value.some(item => item.image_id === editRef.image_id) ? await resolveVersion(editRef) : null
      await makeBase(editImage ?? source, false, true)
      if (imported && same(source, imported)) automaticAnalysisImage = imageRef(source)
      if (props.initialView === 'compare') await openCompare()
      if (props.initialView === 'optimize' && draft.value) draft.value.phase = 'editing'
      if (props.initialView === 'revision' && draft.value) {
        if (visibleInstruction.value) draft.value.phase = 'review'
        else historyInstruction.value = instructions.value[0] ?? null
      }
    }
    publishState()
  } catch (failure) { if (!disposed) error.value = fail(failure) }
  finally { if (!disposed && sequence === initializeSequence) { loading.value = false; void analyzeNewImport() } }
}
function imageLoaded(image: ComicImageView, event?: Event): void {
  if (unavailable(image)) return
  const element = event?.target as HTMLImageElement | undefined
  if (element?.naturalWidth && element.naturalHeight) imageSizes.value[image.artifact_id] = { width: element.naturalWidth, height: element.naturalHeight }
  ready.value[image.artifact_id] = true
  void analyzeNewImport()
}
async function analyzeNewImport(): Promise<void> {
  const image = base.value, pending = automaticAnalysisImage
  if (disposed || loading.value || operation.value || !image || !draft.value
      || unavailable(image) || !same(image, pending) || !ready.value[image.artifact_id]) return
  automaticAnalysisImage = null
  emit('importConsumed', imageRef(image))
  if (autoAnalyze.value && !analysis.value && !previewErrors.value[image.artifact_id]
      && !autoAnalysisStarted.has(key(image))) {
    autoAnalysisStarted.add(key(image)); await analyze(image)
  }
}
async function chooseFamily(id: string): Promise<void> {
  if (loading.value || operation.value) return
  if (!(await saveDraft())) return
  const image = families.value.find(item => item.image_id === id)
  if (!image) return
  loading.value = true; error.value = ''
  try {
    current.value = image; versions.value = []; await loadVersions(id); await preview(image)
    comparison.value = false; comparisonReport.value = null; focus.value = ''; drawing.value = false
    // 换图片（不是换版本）就是换一张要改的图：必须连草稿、分析、指令一起切过去，
    // 否则右侧仍是上一张图的分析，看起来像"两张图结果一样"。
    await makeBase(image)
  }
  catch (failure) { error.value = fail(failure) }
  finally { loading.value = false }
}
async function viewVersion(image: ComicImageView): Promise<void> {
  if (same(otherImage.value, image)) comparisonKey.value = ''
  current.value = image; comparisonReport.value = null
  await preview(image); await savePreference()
}
async function body(): Promise<ComicMediaWrite> {
  const context = await getComicProject(projectId)
  if (!context) throw new Error('作品读取被中断，请重试')
  return { request_id: crypto.randomUUID(), conversation_id: owner, expected_project_version: context.project.current_version }
}
async function perform(name: string, action: () => Promise<void>): Promise<void> {
  if (operation.value) return
  operation.value = name; error.value = ''; feedback.value = ''
  operationErrors.value[name] = ''
  const identity = JSON.stringify(snapshot())
  try { await action(); if (!disposed) emit('changed') }
  catch (failure) { if (!disposed) {
    if (name === 'edit' && identity !== JSON.stringify(snapshot())) { feedback.value = '此前的整理未完成；当前需求已更改，可重新整理。'; return }
    operationErrors.value[name] = name === 'edit' && pendingEdit.value?.uncertain ? '连接中断，原整理请求的结果尚未确认。请核对原请求状态，当前需求已保留。' : fail(failure)
    if (name !== 'edit' && name !== 'analysis') error.value = fail(failure)
    if (name === 'edit' && failure && typeof failure === 'object' && 'reviewDiagnostics' in failure) editDiagnostics.value = failure.reviewDiagnostics as ComicEditDiagnostics | null
  } }
  finally { if (!disposed) operation.value = '' }
}
async function analyze(image = base.value): Promise<void> {
  if (!image || unavailable(image)) return
  const round = draft.value?.round_id, task = draft.value?.task || props.task
  await perform('analysis', async () => {
    const report = await analyzeComicImage(projectId, { ...await body(), image: imageRef(image), task })
    if (!disposed && same(base.value, image) && draft.value && draft.value.round_id === round && draft.value.task === task) {
      analysis.value = report; draft.value!.analysis_artifact_id = report.artifact_id
      draft.value!.selected_finding_ids = []; regions.value = regions.value.filter(item => !item.region_id.startsWith('finding:')).map(item => ({ ...item, finding_id: null }))
      wholeFindingIds.value = []; draft.value.dismissed_finding_region_ids = []; removedSelection.value = null; drawingFinding.value = ''
      edit(); await saveDraft()
    }
  })
}
function toggleFinding(id: string): void {
  if (!draft.value) return
  if (removedSelection.value?.findingId === id) removedSelection.value = null
  const ids = draft.value.selected_finding_ids
  if (ids.includes(id)) { removeRegion('finding:' + id); return }
  else {
    draft.value.selected_finding_ids.push(id)
    draft.value.dismissed_finding_region_ids = (draft.value.dismissed_finding_region_ids ?? []).filter(value => value !== id)
    const item = analysis.value?.output.findings.find(value => value.finding_id === id)
    if (item?.region) regions.value.push({ region_id: 'finding:' + id, label: '区域' + ((analysis.value?.output.findings.indexOf(item) ?? 0) + 1), finding_id: id, bounds: [...item.region], instruction: '' })
  }
  edit()
  expandedItem.value = 'finding:' + id
}
function setFindingScope(id: string, global: boolean): void {
  wholeFindingIds.value = wholeFindingIds.value.filter(value => value !== id)
  if (global) { wholeFindingIds.value.push(id); regions.value = regions.value.filter(item => item.finding_id !== id) }
  else {
    const finding = analysis.value?.output.findings.find(item => item.finding_id === id)
    if (finding?.region && !regions.value.some(item => item.finding_id === id)) regions.value.push({ region_id: 'finding:' + id,
      label: '区域' + (analysis.value!.output.findings.indexOf(finding) + 1), finding_id: id, bounds: [...finding.region], instruction: '' })
  }
  edit()
}
async function drawForFinding(id = ''): Promise<void> {
  await locate('')
  viewTool.value = 'select'
  drawingFinding.value = id; drawing.value = true; expandedItem.value = id ? 'finding:' + id : ''
  feedback.value = id ? '在图片上框选，完成后绑定这条建议。' : '在图片上拖动，添加本轮修改区域。'
}
/* 框选是"改这一版"的入口，不该因为基础图停在其他版本、或上一轮指令还没退回编辑态就变成死按钮：
   正在看哪一版就先以哪一版为修改基础，必要时把已整理好的指令稿退回可编辑，再进入框选。 */
async function toggleDrawing(): Promise<void> {
  if (drawing.value) { drawing.value = false; drawingFinding.value = ''; return }
  const image = current.value
  if (!image || operation.value || loading.value) return
  const rebased = !onBase.value
  if (!(await ensureEditable(image))) return
  await drawForFinding()
  if (rebased && !disposed) feedback.value = `已把 v${image.version} 设为修改基础；在图片上拖动，添加本轮修改区域。`
}
/* 需求卡片里的「框选绑定／重新框选」走同一条准备逻辑，否则同样会静默失效。 */
async function startDrawingFor(findingId: string): Promise<void> {
  const image = current.value
  if (!image || operation.value || loading.value) return
  if (!(await ensureEditable(image))) return
  await drawForFinding(findingId)
}
async function ensureEditable(image: ComicImageView): Promise<boolean> {
  if (!onBase.value) await makeBase(image)
  if (disposed || !onBase.value || !draft.value) return false
  if (draft.value.phase !== 'editing') {
    draft.value.phase = 'editing'; historyInstruction.value = null
    await saveDraft()
  }
  return !disposed && draft.value?.phase === 'editing'
}
function setSupplementTarget(value: string): void {
  instructionTarget.value = value === 'whole' || value === 'selected_regions' ? { mode: value } : { mode: 'region', region_id: value.slice(7) }
  edit()
}
function setRequirementElement(id: string, element: unknown): void {
  if (element) requirementElements.set(id, element as HTMLElement)
  else requirementElements.delete(id)
}
async function revealRequirement(id: string): Promise<void> {
  panelTab.value = 'requirements'
  expandedItem.value = id
  await nextTick()
  requirementElements.get(id)?.scrollIntoView?.({ block: 'nearest' })
}
async function requireCompleteScope(): Promise<boolean> {
  if (pendingModifications.value.length) {
    operationErrors.value.edit = `${pendingModifications.value.length} 条需求尚未确定修改范围，请框选绑定或作为整图要求。`
    await revealRequirement(pendingModifications.value[0]!.id)
    return false
  }
  if (invalidSupplement.value) {
    operationErrors.value.edit = '补充要求的目标选区不存在，请重新选择作用范围。'
    await revealRequirement('supplement')
    return false
  }
  return true
}
async function organize(): Promise<void> {
  if (!base.value || unavailable(base.value) || !draft.value || operation.value || organizing.value || drag) return
  if (pendingEdit.value?.uncertain) { await checkEditRequest(); return }
  organizing.value = true; operationErrors.value.edit = ''; error.value = ''
  try {
    if (!(await requireCompleteScope())) return
    if (!scopeDirty.value) edit(false)
    if (!(await saveDraft())) {
      operationErrors.value.edit = '草稿尚未保存，未提交整理请求。' + saveError.value
      return
    }
    if (!(await requireCompleteScope())) return
    const saved = snapshot()!, fingerprint = JSON.stringify(saved), source = base.value
    if (!saved.edit_scope) throw new Error('请选择整图调整，或框选本轮需要修改的区域。')
    const request: EditRequest = { ...await body(), image: saved.image,
      task: saved.task, round_id: saved.round_id, analysis_artifact_id: saved.analysis_artifact_id,
      selected_finding_ids: saved.selected_finding_ids, instruction: saved.instruction, edit_scope: saved.edit_scope }
    pendingEdit.value = { request, snapshot: fingerprint, uncertain: false }
    await perform('edit', async () => {
      let result: ComicEditInstruction
      try { result = await createComicEditInstruction(projectId, request) }
      catch (failure) {
        if (pendingEdit.value?.request.request_id === request.request_id) pendingEdit.value.uncertain = !(failure && typeof failure === 'object' && 'httpStatus' in failure)
        throw failure
      }
      pendingEdit.value = null
      if (disposed || !same(base.value, source)) return
      instructions.value.unshift(result)
      if (JSON.stringify(snapshot()) !== fingerprint) { feedback.value = '指令已保存到历史；本轮需求已变化，请重新整理。'; return }
      draft.value!.instruction_artifact_id = result.artifact_id; draft.value!.phase = 'review'
      drawing.value = false
      await nextTick()
      if (!disposed) panelElement.value?.scrollIntoView?.({ block: 'start' })
      await saveDraft()
    })
  } catch (failure) {
    if (!disposed) operationErrors.value.edit = fail(failure)
  } finally { organizing.value = false }
}
async function checkEditRequest(recover = false): Promise<void> {
  if (checkingRequest.value || !draft.value || !base.value) return
  checkingRequest.value = true
  const source = base.value, round = draft.value.round_id
  try {
    const tasks = await getComicImageRequests(projectId, owner)
    if (!tasks || disposed || !same(source, base.value) || draft.value?.round_id !== round) return
    const task = tasks.find(item => {
      const input = item.state.media_input as EditRequest | undefined
      return pendingEdit.value ? input?.request_id === pendingEdit.value.request.request_id
        : recover && input?.round_id === round && same(input.image, source)
    })
    if (!task) {
      if (!recover) operationErrors.value.edit = '暂未查到原请求；请保持当前需求，连接恢复后再次核对。系统不会自动重新提交。'
      return
    }
    const request = task.state.media_input as EditRequest
    const matchesDraft = request.task === draft.value.task && request.instruction === draft.value.instruction
      && request.analysis_artifact_id === draft.value.analysis_artifact_id
      && JSON.stringify(request.selected_finding_ids) === JSON.stringify(draft.value.selected_finding_ids)
      && stableScope(request.edit_scope) === stableScope(snapshot()?.edit_scope)
    if (['running', 'pending', 'waiting'].includes(task.status)) {
      pendingEdit.value ??= { request, snapshot: matchesDraft ? JSON.stringify(snapshot()) : '', uncertain: true }
      operationErrors.value.edit = '原整理请求尚未结束；请核对原请求状态，避免重复提交。'
      return
    }
    pendingEdit.value = null
    if (task.status === 'completed') {
      const currentDraft = JSON.stringify(snapshot())
      const reports = await getComicImageReports<ComicEditInstruction>(projectId, owner, 'edit-instructions', source)
      if (disposed || !same(source, base.value) || draft.value?.round_id !== round) return
      const result = reports?.items.find(item => item.run_id === task.id)
      if (result) {
        if (!instructions.value.some(item => item.artifact_id === result.artifact_id)) instructions.value.unshift(result)
        const unchanged = matchesDraft && currentDraft === JSON.stringify(snapshot())
        if (unchanged) { draft.value.instruction_artifact_id = result.artifact_id; draft.value.phase = 'review'; await saveDraft() }
        feedback.value = unchanged ? '已恢复原请求生成的指令。' : '原指令已保留在历史，当前需求可以重新整理。'
        operationErrors.value.edit = ''
      } else {
        pendingEdit.value = { request, snapshot: currentDraft, uncertain: true }
        operationErrors.value.edit = '原请求已完成，尚未读取到指令；请再次核对，不会自动重新提交。'
      }
    } else if (matchesDraft) {
      operationErrors.value.edit = task.error ?? '原整理请求已停止，需求和选区已保留。'
      editDiagnostics.value = task.state.review_diagnostics as ComicEditDiagnostics | null ?? null
    }
  } catch (failure) { if (!disposed) operationErrors.value.edit = '原请求状态读取失败：' + fail(failure) }
  finally { checkingRequest.value = false }
}
async function confirmAndCopy(): Promise<void> {
  const instruction = visibleInstruction.value
  if (!instruction) return
  await perform('confirm', async () => {
    if (instruction.requires_confirmation) {
      await confirmComicEditInstruction(projectId, instruction.artifact_id, await body())
      instruction.requires_confirmation = false
    }
    if (disposed) return
    try { await navigator.clipboard.writeText(finalImagePrompt(instruction.output)); feedback.value = '指令已复制，请在外部工具附上原图。' }
    catch { feedback.value = '指令已确认；浏览器未允许复制，请选中正文手动复制。' }
  })
}
function stageImport(file: File): void {
  if (restoring.value) { restoring.value = false; void restoreOriginal(file); return }
  pendingImport = null; importInstructions.value = []; importCursor.value = null
  importFile.value = file; importMode.value = current.value && !unavailable(current.value) ? 'version' : 'new'
  importParent.value = current.value ? key(current.value) : ''
  importInstructionId.value = visibleInstruction.value && !visibleInstruction.value.requires_confirmation
    && same(visibleInstruction.value.basis.source_image, targetParent.value) ? visibleInstruction.value.artifact_id : ''
  associateInstruction.value = !!parentInstruction.value
  void refreshImportInstructions()
}
async function refreshImportInstructions(more = false): Promise<void> {
  const parent = targetParent.value
  if (!parent) return
  const target = key(parent)
  try {
    const page = await getComicImageReports<ComicEditInstruction>(projectId, owner, 'edit-instructions', parent, more ? importCursor.value ?? undefined : undefined)
    if (page && !disposed && target === importParent.value) {
      importInstructions.value = more ? [...importInstructions.value, ...page.items] : page.items
      importCursor.value = page.next_cursor
    }
  } catch (failure) { if (!disposed) error.value = fail(failure) }
}
async function finishImport(): Promise<void> {
  if (!importFile.value) return
  const file = importFile.value, parent = targetParent.value, link = parentInstruction.value
  if (importMode.value === 'version' && !parent) { error.value = '请选择该文件实际基于哪个版本修改。'; return }
  if (importMode.value === 'version' && associateInstruction.value && !link) { error.value = '请选择实际使用的已确认指令，或取消关联。'; return }
  if (!(await saveDraft())) return
  let imported: ComicImageView | null = null
  const fingerprint = JSON.stringify({ mode: importMode.value, parent: parent ? imageRef(parent) : null,
    instruction: associateInstruction.value ? link?.artifact_id : null })
  await perform('upload', async () => {
    if (importMode.value === 'new' && props.briefVersion !== undefined) {
      const context = await getComicProject(projectId)
      if (context?.creative_brief.version !== props.briefVersion)
        throw new Error('这是保留的历史图片作品。可追加原图片的新版本；新素材请在新的作品中导入。')
    }
    if (!pendingImport || pendingImport.file !== file || pendingImport.fingerprint !== fingerprint) {
      let versionFields = {}
      if (importMode.value === 'version' && parent) {
        const latest = await getComicImageVersions(parent.image_id, owner)
        if (!latest?.versions[0]) throw new Error('图片版本无法读取')
        versionFields = { image_id: parent.image_id, expected_image_version: latest.versions[0].version, parent: imageRef(parent),
          ...(associateInstruction.value && link ? { instruction_artifact_id: link.artifact_id } : {}) }
      }
      pendingImport = { file, fingerprint, body: { ...await body(), ...versionFields } }
    }
    imported = await importComicImage(projectId, file, pendingImport.body)
    if (disposed) return
    families.value = [...families.value.filter(item => item.image_id !== imported!.image_id),
      { ...imported!, selection: families.value.find(item => item.image_id === imported!.image_id)?.selection }]
    current.value = imported; await loadVersions(imported.image_id); await makeBase(imported)
    importFile.value = null; pendingImport = null; feedback.value = '图片已导入；正式采用需要另行选择。'
    publishState()
  })
  // Only a fresh import initiates automatic analysis, after actual browser image decoding.
  const source = imported as ComicImageView | null
  if (source && !disposed && same(source, base.value)) {
    automaticAnalysisImage = imageRef(source); await analyzeNewImport()
  }
}
async function restoreOriginal(file: File): Promise<void> {
  const image = current.value
  if (!image) return
  await perform('restore', async () => {
    await restoreComicImageFile(imageRef(image), file, await body())
    if (!disposed) { await initialize(); feedback.value = '原文件已恢复，原有版本关系保留。' }
  })
}
function askDeleteImage(): void {
  if (!current.value || operation.value || loading.value) return
  deleteTarget.value = current.value
}
async function confirmDeleteImage(): Promise<void> {
  const image = deleteTarget.value
  if (!image) return
  // 待保存的草稿属于这张图片，删除后不再有落点，先掐掉防抖保存。
  clearTimeout(saveTimer)
  await perform('delete', async () => {
    await deleteComicImage(projectId, image.image_id, await body())
    if (disposed) return
    deleteTarget.value = null
    families.value = families.value.filter(item => item.image_id !== image.image_id)
    versions.value = []; current.value = null; base.value = null; draft.value = null
    analysis.value = null; instructions.value = []; comparison.value = false
    feedback.value = '图片已删除；如需找回，可在素材库回收站恢复。'
    await initialize()
  })
}
async function adopt(): Promise<void> {
  const image = current.value, family = selectedFamily.value
  if (!image || !family || unavailable(image)) return
  await perform('select', async () => {
    await selectComicImage(image.image_id, { ...await body(), version: image.version,
      expected_selection_version: family.selection?.selection_version ?? 0 })
    if (!disposed) {
      family.selection = { selected_version: image.version, selection_version: (family.selection?.selection_version ?? 0) + 1 }
      feedback.value = '已采用 v' + image.version + '；其他版本仍保留。'
      publishState()
    }
  })
}
/* —— 版本比较视图：上方两个大图框 + 底部图片池 ——
   a 槽沿用 comparisonKey（它同时驱动 otherImage / canCheck / 核验报告），
   b 槽沿用 current（viewVersion 会把 current 一并切过去，离开比较视图后状态依然一致）。
   进入比较视图时两槽刻意留空，交给用户从下方图片池点选或拖入，
   所以额外用 compareBPicked 记录"b 槽是否已被用户选过"。 */
const compareSlots = ['a', 'b'] as const
const compareBPicked = ref(false), dropTarget = ref<'a' | 'b' | ''>('')
const slotA = computed(() => otherImage.value)
const slotB = computed(() => (compareBPicked.value ? current.value : null))
const poolImages = computed(() => allVersions.value)
const slotImage = (slot: 'a' | 'b'): ComicImageView | null => (slot === 'a' ? slotA.value : slotB.value)
const slotLabel = (slot: 'a' | 'b'): string => (slot === 'a' ? '对照版本' : '当前查看')
const slotOf = (image: ComicImageView): 'a' | 'b' | '' =>
  (slotA.value && same(slotA.value, image)) ? 'a' : (slotB.value && same(slotB.value, image)) ? 'b' : ''
/* 槽位渲染描述：把可空判断都收在脚本里，模板中就不必写非空断言。 */
const compareViews = computed(() => compareSlots.map(slot => {
  const image = slotImage(slot), id = image?.artifact_id ?? ''
  return { slot, label: slotLabel(slot), hasImage: !!image,
    meta: image ? 'v' + image.version + (image.parent ? ' · 来自 v' + image.parent.version : ' · 原始导入') : '',
    url: !image || previewErrors.value[id] ? '' : previewUrl(image),
    error: image ? previewErrors.value[id] ?? '' : '' }
}))
function markSlotError(slot: 'a' | 'b'): void {
  const image = slotImage(slot)
  if (image) decodeError(image)
}
function retrySlot(slot: 'a' | 'b'): void {
  const image = slotImage(slot)
  if (image) void preview(image, true)
}
async function placeInCompare(image: ComicImageView, slot: 'a' | 'b'): Promise<void> {
  if (slot === 'a') { await chooseComparison(key(image)); return }
  compareBPicked.value = true; comparisonReport.value = null; await viewVersion(image)
}
/* 点选规则：先填左槽，左槽有人就填右槽；两槽都满时替换左槽，
   这样"从左到右依次填入"的直觉在重复点选时依然成立。 */
function pickForCompare(image: ComicImageView): void {
  if (slotOf(image)) return
  void placeInCompare(image, !slotA.value ? 'a' : !slotB.value ? 'b' : 'a')
}
function clearCompareSlot(slot: 'a' | 'b'): void {
  comparisonReport.value = null
  if (slot === 'a') { comparisonKey.value = ''; return }
  compareBPicked.value = false
}
function startCompareDrag(event: DragEvent, image: ComicImageView): void {
  if (!event.dataTransfer) return
  event.dataTransfer.setData('text/plain', key(image)); event.dataTransfer.effectAllowed = 'copy'
}
function dragOverCompare(event: DragEvent, slot: 'a' | 'b'): void {
  event.preventDefault(); dropTarget.value = slot
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
}
function dragLeaveCompare(slot: 'a' | 'b'): void { if (dropTarget.value === slot) dropTarget.value = '' }
function dropOnCompare(event: DragEvent, slot: 'a' | 'b'): void {
  event.preventDefault(); dropTarget.value = ''
  const found = allVersions.value.find(item => key(item) === (event.dataTransfer?.getData('text/plain') ?? ''))
  if (found) void placeInCompare(found, slot)
}
async function openCompare(): Promise<void> {
  if (!current.value) return
  drawing.value = false; comparison.value = !comparison.value; comparisonReport.value = null
  if (!comparison.value) return
  // 两槽先留空，等用户从下方图片池点选或直接拖入；不再替用户预选对照版本。
  comparisonKey.value = ''; compareBPicked.value = false; dropTarget.value = ''
  // 图片池要把缩略图都显示出来，而预览是惰性加载的；prunePreviews 上限 12，按同量级预热。
  await Promise.all(poolImages.value.slice(0, 12).map(item => preview(item)))
}
async function chooseComparison(value: string): Promise<void> {
  comparisonKey.value = value; comparisonReport.value = null
  if (otherImage.value) await preview(otherImage.value)
}
const canCheck = computed(() => !unavailable(current.value) && !unavailable(otherImage.value) && !!current.value?.instruction_artifact_id && same(current.value?.parent ?? null, otherImage.value))
async function checkComparison(): Promise<void> {
  const before = otherImage.value, after = current.value
  if (!before || !after?.instruction_artifact_id || !canCheck.value) return
  await perform('compare', async () => {
    const report = await compareComicImages(projectId, { ...await body(), before: imageRef(before), after: imageRef(after), instruction_artifact_id: after.instruction_artifact_id! })
    if (!disposed && same(after, current.value) && same(before, otherImage.value)) comparisonReport.value = report
  })
}
async function moreInstructions(): Promise<void> {
  if (!base.value || !reportCursor.value) return
  const source = base.value
  try {
    const page = await getComicImageReports<ComicEditInstruction>(projectId, owner, 'edit-instructions', source, reportCursor.value)
    if (page && !disposed && same(source, base.value)) { instructions.value.push(...page.items); reportCursor.value = page.next_cursor }
  } catch (failure) { error.value = fail(failure) }
}
const regionStyle = (bounds: number[]) => ({ left: bounds[0]! * 100 + '%', top: bounds[1]! * 100 + '%',
  width: (bounds[2]! - bounds[0]!) * 100 + '%', height: (bounds[3]! - bounds[1]!) * 100 + '%' })
type Drag = { id: string; corner: string; start: number[]; point: number[]; pointer: number; creating: boolean }
let drag: Drag | null = null
function point(event: PointerEvent): number[] {
  const rect = boundsElement.value!.getBoundingClientRect()
  return [Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
    Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height))]
}
function startDraw(event: PointerEvent): void {
  if (viewTool.value === 'pan' || !drawing.value || !onBase.value || !current.value || !ready.value[current.value.artifact_id] || draft.value?.phase !== 'editing' || event.button !== 0) return
  clearTimeout(saveTimer)
  const start = point(event), id = crypto.randomUUID()
  const finding = analysis.value?.output.findings.find(item => item.finding_id === drawingFinding.value)
  if (finding) wholeFindingIds.value = wholeFindingIds.value.filter(id => id !== finding.finding_id)
  const number = 1 + Math.max(0, ...regions.value.map(item => Number(item.label?.match(/^手动(\d+)$/)?.[1] ?? 0)))
  regions.value.push({ region_id: id, label: '手动' + number, bounds: [start[0]!, start[1]!, start[0]!, start[1]!], instruction: '', finding_id: finding?.finding_id ?? null })
  drag = { id, corner: 'se', start: [...start, ...start], point: start, pointer: event.pointerId, creating: true }
  focus.value = id; boundsElement.value?.setPointerCapture(event.pointerId); event.preventDefault()
}
function startMove(event: PointerEvent, id: string, corner = ''): void {
  if (viewTool.value === 'pan') { startPan(event); return }
  if (drawing.value) { startDraw(event); return }
  if (draft.value?.phase !== 'editing' || event.button !== 0) return
  focus.value = id
  selectRegionRequirement(id)
  const item = regions.value.find(region => region.region_id === id)
  if (!item) return
  clearTimeout(saveTimer)
  drag = { id, corner, start: [...item.bounds], point: point(event), pointer: event.pointerId, creating: false }
  boundsElement.value?.setPointerCapture(event.pointerId); event.preventDefault()
}
function move(event: PointerEvent): void {
  if (!drag || drag.pointer !== event.pointerId) return
  const item = regions.value.find(region => region.region_id === drag!.id)
  if (!item) return
  const p = point(event), b = [...drag.start], dx = p[0]! - drag.point[0]!, dy = p[1]! - drag.point[1]!
  if (drag.creating) item.bounds = [Math.min(drag.point[0]!, p[0]!), Math.min(drag.point[1]!, p[1]!), Math.max(drag.point[0]!, p[0]!), Math.max(drag.point[1]!, p[1]!)]
  else if (!drag.corner) {
    const x = Math.min(1 - b[2]!, Math.max(-b[0]!, dx)), y = Math.min(1 - b[3]!, Math.max(-b[1]!, dy))
    item.bounds = [b[0]! + x, b[1]! + y, b[2]! + x, b[3]! + y]
  } else {
    if (drag.corner.includes('w')) b[0] = Math.min(b[2]! - .006, Math.max(0, b[0]! + dx))
    if (drag.corner.includes('e')) b[2] = Math.max(b[0]! + .006, Math.min(1, b[2]! + dx))
    if (drag.corner.includes('n')) b[1] = Math.min(b[3]! - .006, Math.max(0, b[1]! + dy))
    if (drag.corner.includes('s')) b[3] = Math.max(b[1]! + .006, Math.min(1, b[3]! + dy))
    item.bounds = b
  }
  edit(false)
}
function endMove(event: PointerEvent, cancelled = false): void {
  if (!drag || drag.pointer !== event.pointerId) return
  const prior = drag, item = regions.value.find(region => region.region_id === prior.id)
  if (item && (cancelled || item.bounds[2]! - item.bounds[0]! < .006 || item.bounds[3]! - item.bounds[1]! < .006)) {
    if (prior.creating) regions.value = regions.value.filter(region => region.region_id !== prior.id)
    else item.bounds = prior.start
  }
  drag = null; boundsElement.value?.releasePointerCapture(event.pointerId); drawing.value = false; drawingFinding.value = ''
  if (prior.creating || (item && JSON.stringify(prior.start) !== JSON.stringify(item.bounds))) edit()
  if (item) selectRegionRequirement(item.region_id)
}
function removeRegion(id: string): void {
  if (!draft.value || draft.value.phase !== 'editing') return
  const item = regions.value.find(region => region.region_id === id)
  const findingId = item?.finding_id ?? (id.startsWith('finding:') ? id.slice(8) : null)
  if (!item && !analysis.value?.output.findings.some(finding => finding.finding_id === findingId)) return
  removedSelection.value = { round: draft.value.round_id, findingId,
    regions: regions.value.filter(region => findingId ? region.finding_id === findingId : region.region_id === id).map(region => ({ ...region, bounds: [...region.bounds] })),
    selected: !!findingId && draft.value.selected_finding_ids.includes(findingId),
    whole: !!findingId && wholeFindingIds.value.includes(findingId),
    dismissed: !!findingId && !!draft.value.dismissed_finding_region_ids?.includes(findingId) }
  regions.value = regions.value.filter(region => findingId ? region.finding_id !== findingId : region.region_id !== id)
  if (findingId) {
    draft.value.selected_finding_ids = draft.value.selected_finding_ids.filter(value => value !== findingId)
    wholeFindingIds.value = wholeFindingIds.value.filter(value => value !== findingId)
    draft.value.dismissed_finding_region_ids = [...new Set([...(draft.value.dismissed_finding_region_ids ?? []), findingId])]
  }
  if (drawingFinding.value === findingId) { drawingFinding.value = ''; drawing.value = false }
  focus.value = ''; expandedItem.value = ''
  edit()
  removedSelection.value.round = draft.value.round_id
}
function undoRemoval(): void {
  const removed = removedSelection.value
  if (!removed || !draft.value || removed.round !== draft.value.round_id) return
  regions.value.push(...removed.regions.filter(item => !regions.value.some(region => region.region_id === item.region_id)))
  if (removed.findingId) {
    if (removed.selected && !draft.value.selected_finding_ids.includes(removed.findingId)) draft.value.selected_finding_ids.push(removed.findingId)
    if (removed.whole && !wholeFindingIds.value.includes(removed.findingId)) wholeFindingIds.value.push(removed.findingId)
    if (!removed.dismissed) draft.value.dismissed_finding_region_ids = (draft.value.dismissed_finding_region_ids ?? []).filter(id => id !== removed.findingId)
  }
  removedSelection.value = null; edit()
}
function linkRegion(item: ComicEditRegion, id: string): void {
  item.finding_id = id || null
  if (id && draft.value) {
    if (!draft.value.selected_finding_ids.includes(id)) draft.value.selected_finding_ids.push(id)
    wholeFindingIds.value = wholeFindingIds.value.filter(value => value !== id)
    draft.value.dismissed_finding_region_ids = (draft.value.dismissed_finding_region_ids ?? []).filter(value => value !== id)
    if (removedSelection.value?.findingId === id) removedSelection.value = null
    expandedItem.value = 'finding:' + id
  }
  edit()
}
function bindExistingRegion(findingId: string, regionId: string): void {
  const item = regions.value.find(region => region.region_id === regionId)
  if (item) linkRegion(item, findingId)
}
function nudge(event: KeyboardEvent, id: string): void {
  if (draft.value?.phase !== 'editing') return
  if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); removeRegion(id); return }
  const item = regions.value.find(region => region.region_id === id)
  if (!item) return
  if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return
  event.preventDefault()
  const amount = event.shiftKey ? .02 : .002, b = item.bounds
  const dx = event.key === 'ArrowLeft' ? -amount : event.key === 'ArrowRight' ? amount : 0
  const dy = event.key === 'ArrowUp' ? -amount : event.key === 'ArrowDown' ? amount : 0
  const x = Math.min(1 - b[2]!, Math.max(-b[0]!, dx)), y = Math.min(1 - b[3]!, Math.max(-b[1]!, dy))
  item.bounds = [b[0]! + x, b[1]! + y, b[2]! + x, b[3]! + y]; edit()
}
function updateTask(): void {
  if (!draft.value) return
  draft.value.analysis_artifact_id = null; draft.value.selected_finding_ids = []
  wholeFindingIds.value = []; draft.value.dismissed_finding_region_ids = []; removedSelection.value = null; drawingFinding.value = ''
  regions.value = regions.value.filter(item => !item.region_id.startsWith('finding:')).map(item => ({ ...item, finding_id: null })); analysis.value = null; edit()
}
function useManualInput(): void {
  if (!draft.value) return
  draft.value.analysis_artifact_id = null; draft.value.selected_finding_ids = []; analysis.value = null
  wholeFindingIds.value = []; draft.value.dismissed_finding_region_ids = []; removedSelection.value = null; drawingFinding.value = ''
  regions.value = regions.value.map((item, index) => {
    const regionId = item.region_id.startsWith('finding:') ? crypto.randomUUID() : item.region_id
    if (instructionTarget.value.mode === 'region' && instructionTarget.value.region_id === item.region_id)
      instructionTarget.value = { mode: 'region', region_id: regionId }
    return { ...item, region_id: regionId, label: '手动' + (index + 1), finding_id: null }
  })
  edit(); feedback.value = '已切换为人工修改，保留当前选区；修改要求由你填写。'
}
async function locate(id: string): Promise<void> {
  focus.value = id; comparison.value = false
  if (base.value) {
    const otherFamily = current.value?.image_id !== base.value.image_id
    current.value = base.value
    if (otherFamily) await loadVersions(base.value.image_id)
    await preview(base.value); void savePreference()
  }
  selectRegionRequirement(id)
  await nextTick(); measureView()
  const region = overlays.value.find(item => item.id === id)
  if (region && currentSize.value && fitScale.value) {
    const [x1, y1, x2, y2] = region.bounds as [number, number, number, number]
    zoom.value = Math.min(16, Math.max(1, Math.min(
      Math.max(1, canvasSize.value.width - 64) / ((x2 - x1) * currentSize.value.width),
      Math.max(1, canvasSize.value.height - 64) / ((y2 - y1) * currentSize.value.height)) / fitScale.value))
    pan.value = { x: (.5 - (x1 + x2) / 2) * displaySize.value.width,
      y: (.5 - (y1 + y2) / 2) * displaySize.value.height }
    boundPan()
  }
}
function selectRegionRequirement(id: string): void {
  const region = regions.value.find(item => item.region_id === id)
  const requirementId = region?.finding_id ? 'finding:' + region.finding_id : id
  if (modifications.value.some(item => item.id === requirementId)) { panelTab.value = 'requirements'; expandedItem.value = requirementId }
  else if (id.startsWith('finding:')) panelTab.value = 'analysis'
}
function selectRequirement(id: string): void {
  expandedItem.value = id; panelTab.value = 'requirements'
  const item = modifications.value.find(item => item.id === id)
  focus.value = item?.regions[0]?.region_id ?? ''
}
function diagnosticTarget(issue: ComicEditDiagnostics['issues'][number]): string {
  if (issue.requirement_ids.includes('user-instruction')) return 'supplement'
  if (issue.finding_ids[0]) return 'finding:' + issue.finding_ids[0]
  const region = regions.value.find(item => item.region_id === issue.region_ids[0])
  return region?.finding_id ? 'finding:' + region.finding_id : region?.region_id ?? ''
}
function publishState(): void {
  if (disposed) return
  emit('state', { projectId, conversationId: owner, hasImages: families.value.length > 0,
    adopted: families.value.some(item => !!item.selection?.selected_version && item.selection.available !== false) })
}
onMounted(initialize)
onBeforeUnmount(() => {
  resizeObserver?.disconnect(); panDrag = null
  clearTimeout(saveTimer)
  if (drag) {
    const item = regions.value.find(region => region.region_id === drag!.id)
    if (drag.creating) regions.value = regions.value.filter(region => region.region_id !== drag!.id)
    else if (item) item.bounds = drag.start
    drag = null
  }
  if (draft.value) void saveDraft()
  disposed = true; selectionEpoch++
  Object.values(urls.value).forEach(url => URL.revokeObjectURL(url))
})
</script>

<template>
  <section ref="workbenchElement" class="image-workbench" :class="{ 'is-compact': compact }" aria-label="图片工作台">
    <p v-if="loading" role="status">正在恢复图片工作台…</p>
    <p v-if="error" class="workspace-error" role="alert">{{ error }}<button v-if="!draft" class="ui-button quiet sm" @click="initialize">重新读取工作台</button></p>
    <div class="wb-layout">
      <div class="wb-main">
        <!-- 视图栏兼工作台工具栏：原先独立的提示栏已并入，省下一条横向占位给画布。
             左半是"看什么"，右半是"怎么处理"。 -->
        <div class="wb-viewbar">
          <div class="wb-viewbar-left">
            <label v-if="families.length">图片 <select :value="current?.image_id" :disabled="!!operation || loading" @change="chooseFamily(($event.target as HTMLSelectElement).value)">
              <option v-for="image in families" :key="image.image_id" :value="image.image_id">{{ image.label || image.filename }}</option>
            </select></label>
            <span v-if="current">查看 v{{ current.version }}{{ unavailable(current) ? ' · 图片不可用' : adopted === current.version ? ' · 已采用' : '' }}{{ !onBase && base ? ` · 修改基础 v${base.version}${unavailable(base) ? '（不可用）' : ''}` : '' }}</span>
          </div>
          <div class="wb-actions">
            <button class="ui-button quiet sm" :disabled="!current" @click="openCompare"><Columns2 :size="14" />{{ comparison ? '返回单图' : '比较版本' }}</button>
            <button v-if="comparison" class="ui-button sm" :disabled="!!operation || !canCheck"
              :title="canCheck ? '按已确认指令核对修改效果' : '按指令检查需要其实际来源版本、已确认指令和关联的修改结果'"
              @click="checkComparison">{{ operation === 'compare' ? '正在检查…' : '检查修改效果' }}</button>
            <button class="ui-button quiet sm" :disabled="!current || !ready[current.artifact_id] || comparison || !!operation || loading" :aria-pressed="drawing"
              :title="drawing ? '结束框选' : onBase ? '在图片上拖动框选区域' : `在 v${current?.version} 上框选；会先把这一版设为修改基础`"
              @click="toggleDrawing"><Maximize :size="14" />框选区域</button>
            <button class="wb-auto-pill" type="button" :aria-pressed="autoAnalyze" @click="toggleAutoAnalyze">导入后分析</button>
          </div>
        </div>
        <!-- 框选提示从画布里搬出来：原先浮在图片顶部，图片一满屏就盖住画面内容。 -->
        <p v-if="drawing" class="wb-draw-status" role="status"><Maximize :size="13" />在图片上拖动框选；选中后可移动、缩放或用方向键微调。</p>
        <ComicImageImport class="wb-import" :disabled="!!operation || loading" :uploading="operation === 'upload'" :label="restoring ? '选择缺失的原文件' : '导入图片'" @upload="stageImport">
          <template #actions>
            <button class="ui-button quiet sm wb-import-delete" :disabled="!!operation || loading || !current || comparison || !!importFile"
              :title="current ? '删除这张图片及其全部版本' : '还没有可删除的图片'" @click="askDeleteImage"><Trash2 :size="14" />删除图片</button>
          </template>
          <div v-if="comparison" class="wb-compare">
            <div class="wb-compare-stage">
              <section v-for="view in compareViews" :key="view.slot" class="wb-compare-slot"
                :class="{ 'is-filled': view.hasImage, 'is-drop': dropTarget === view.slot }"
                :aria-label="view.label"
                @dragover="dragOverCompare($event, view.slot)" @dragleave="dragLeaveCompare(view.slot)" @drop="dropOnCompare($event, view.slot)">
                <header class="wb-compare-head">
                  <span class="wb-compare-tag" aria-hidden="true">{{ view.slot === 'a' ? 'A' : 'B' }}</span>
                  <strong>{{ view.label }}</strong>
                  <small v-if="view.hasImage">{{ view.meta }}</small>
                  <button v-if="view.hasImage" class="wb-compare-clear" type="button" :aria-label="'移出' + view.label" @click="clearCompareSlot(view.slot)"><X :size="13" /></button>
                </header>
                <div class="wb-compare-body">
                  <ImageViewport v-if="view.url" :src="view.url" :alt="view.label" @error="markSlotError(view.slot)" />
                  <div v-else class="wb-compare-placeholder">
                    <span class="wb-compare-placeholder-icon"><ImagePlus :size="22" /></span>
                    <strong>{{ view.error ? '图片无法显示' : view.hasImage ? '正在读取图片…' : '放入图片' }}</strong>
                    <small>{{ view.error || (view.hasImage ? '请稍候' : '点击下方图片池选择，或直接把图片拖到这里') }}</small>
                    <button v-if="view.error" class="ui-button quiet sm" @click="retrySlot(view.slot)">重试读取</button>
                  </div>
                </div>
              </section>
            </div>
            <div class="wb-compare-pool">
              <div class="wb-compare-pool-head">
                <strong>全部图片</strong>
                <small>点击图片按「左 → 右」填入空位，也可以直接拖进上面的框</small>
                <button v-if="nextVersion" class="ui-button quiet sm" @click="loadVersions(current!.image_id, true)">加载更早版本</button>
              </div>
              <div class="wb-compare-pool-list">
                <button v-for="image in poolImages" :key="key(image)" type="button" class="wb-compare-thumb"
                  :class="{ 'is-used': !!slotOf(image) }" :aria-pressed="!!slotOf(image)"
                  draggable="true" @dragstart="startCompareDrag($event, image)" @click="pickForCompare(image)">
                  <span class="wb-compare-thumb-img">
                    <img v-if="previewUrl(image) && !previewErrors[image.artifact_id]" :src="previewUrl(image)" :alt="'v' + image.version" loading="lazy" @error="decodeError(image)" />
                    <ImageOff v-else :size="16" />
                  </span>
                  <span class="wb-compare-thumb-text"><strong>v{{ image.version }}</strong><small>{{ slotOf(image) === 'a' ? '已在左侧' : slotOf(image) === 'b' ? '已在右侧' : image.parent ? '来自 v' + image.parent.version : '原始导入' }}</small></span>
                </button>
              </div>
            </div>
          </div>
          <div v-else ref="canvasElement" class="wb-canvas" :class="{ 'is-drawing': drawing, 'is-panning': viewTool === 'pan', 'is-dragging': panning }"
            @pointerdown="startPan" @pointermove="movePan" @pointerup="endPan" @pointercancel="endPan" @wheel.prevent="wheelZoom">
            <div v-if="current && ready[current.artifact_id]" class="wb-view-tools" aria-label="图片查看工具" @pointerdown.stop>
              <button class="ui-button quiet sm" :aria-pressed="viewTool === 'select' && !drawing" aria-label="选择和调整区域" @click="viewTool = 'select'; drawing = false"><MousePointer2 :size="16" /></button>
              <button class="ui-button quiet sm" :aria-pressed="viewTool === 'pan'" aria-label="拖动画布" @click="viewTool = 'pan'; drawing = false"><Hand :size="16" /></button>
              <button class="ui-button quiet sm" aria-label="缩小图片" @click="changeZoom(1 / 1.2)"><Minus :size="16" /></button>
              <button class="wb-zoom-reset" type="button" aria-label="重置缩放为 100%" @click="resetZoom">{{ Math.round(zoom * 100) }}%</button>
              <button class="ui-button quiet sm" aria-label="放大图片" @click="changeZoom(1.2)"><Plus :size="16" /></button>
              <button class="ui-button quiet sm" @click="fitView">适应画布</button>
            </div>
            <template v-if="current && previewUrl(current) && !previewErrors[current.artifact_id]">
              <div class="wb-viewport" :style="viewportStyle">
              <div ref="boundsElement" class="wb-image-bounds" :style="imageStyle" @pointerdown="startDraw" @pointermove="move" @pointerup="endMove" @pointercancel="endMove($event, true)">
                <img :src="previewUrl(current)" :alt="current.filename" draggable="false" @load="imageLoaded(current, $event)" @error="decodeError(current)" />
                <div class="wb-overlay">
                  <div v-for="region in overlays" :key="region.id" class="wb-region" :class="{ selected: region.selected, focused: focus === region.id }"
                    :style="regionStyle(region.bounds)" role="button" tabindex="0" :aria-label="'区域 ' + region.number"
                    @pointerdown.stop="startMove($event, region.id)" @keydown="nudge($event, region.id)" @focus="focus = region.id; selectRegionRequirement(region.id)">
                    <span class="wb-region-number">{{ region.number }}</span>
                    <button v-if="focus === region.id && draft?.phase === 'editing' && viewTool === 'select'" class="wb-region-delete" :aria-label="'删除区域 ' + region.number" @pointerdown.stop @click.stop="removeRegion(region.id)"><X :size="14" /></button>
                    <button v-for="corner in ['nw', 'ne', 'sw', 'se']" v-show="focus === region.id && region.selected && draft?.phase === 'editing' && viewTool === 'select'" :key="corner"
                      class="wb-handle" :class="corner" :aria-label="'调整区域 ' + region.number + ' 的 ' + corner + ' 角'" @pointerdown.stop="startMove($event, region.id, corner)" />
                  </div>
                </div>
              </div>
              </div>
            </template>
            <div v-else class="wb-empty">
              <ImageOff :size="28" />
              <p>{{ current ? previewErrors[current.artifact_id] || (error ? '图片读取未完成，请重新读取工作台或导入图片。' : '正在读取图片…') : '导入第一张图片开始修改' }}</p>
              <template v-if="current && previewErrors[current.artifact_id]">
                <button v-if="unavailable(current)" class="ui-button sm" @click="initialize">重新读取工作台</button>
                <button v-else class="ui-button sm" @click="preview(current, true)">重试读取</button>
                <button v-if="!current.lifecycle_state || current.lifecycle_state === 'active'" class="ui-button quiet sm" :disabled="!!operation" @click="restoring = true">重传缺失原文件</button>
                <small>{{ recoveryHint(current) }}</small>
              </template>
            </div>
          </div>
        </ComicImageImport>
        <div v-if="importFile" class="wb-import-confirm" role="dialog" aria-label="导入确认">
          <strong>导入 {{ importFile.name }}</strong>
          <label><input v-model="importMode" type="radio" value="new" />独立图片</label>
          <label v-if="current && !unavailable(current)"><input v-model="importMode" type="radio" value="version" />现有图片的新版本</label>
          <label v-if="importMode === 'version'">实际来源
            <select v-model="importParent" @change="refreshImportInstructions()"><option v-for="image in allVersions" :key="key(image)" :value="key(image)" :disabled="unavailable(image)">v{{ image.version }} · {{ image.filename }}{{ unavailable(image) ? '（不可用）' : '' }}</option></select>
          </label>
          <template v-if="importMode === 'version' && parentInstructions.length">
            <label><input v-model="associateInstruction" type="checkbox" />关联已确认指令（仅用于按此指令完成的结果）</label>
            <select v-if="associateInstruction" v-model="importInstructionId"><option value="">选择实际使用的指令</option><option v-for="item in parentInstructions" :key="item.artifact_id" :value="item.artifact_id">{{ item.output.summary }}</option></select>
          </template>
          <small v-else-if="importMode === 'version'">未关联指令的版本仍可比较查看，不进行指令达成核验。</small>
          <button v-if="importMode === 'version' && importCursor" class="ui-button quiet sm" @click="refreshImportInstructions(true)">加载更早的已确认指令</button>
          <details v-if="associateInstruction && parentInstruction"><summary>核对将关联的指令</summary><pre class="wb-instruction">{{ finalImagePrompt(parentInstruction.output) }}</pre></details>
          <div class="wb-actions"><button class="ui-button primary sm" :disabled="!!operation" @click="finishImport">确认导入</button><button class="ui-button quiet sm" @click="importFile = null">取消</button></div>
        </div>
        <div v-if="deleteTarget" class="wb-import-confirm" role="dialog" aria-label="删除图片确认">
          <strong>删除 {{ deleteTarget.filename }}</strong>
          <small>这张图片的全部版本、采用状态与逐版本改图草稿都会移除，图片同时进入素材库回收站。已固定在故事编排或正作为参考资产使用的图片不能删除。</small>
          <div class="wb-actions">
            <button class="ui-button danger sm" :disabled="!!operation" @click="confirmDeleteImage">{{ operation === 'delete' ? '正在删除…' : '确认删除' }}</button>
            <button class="ui-button quiet sm" @click="deleteTarget = null">取消</button>
          </div>
        </div>
        <div v-if="current" class="wb-actions wb-image-actions">
          <button class="ui-button sm" :disabled="!!operation || unavailable(current) || !ready[current.artifact_id] || adopted === current.version" @click="adopt"><Check :size="14" />{{ unavailable(current) ? '图片不可用' : adopted === current.version ? '当前已采用' : '采用此版本' }}</button>
          <button v-if="!onBase" class="ui-button primary sm" :disabled="!!operation || unavailable(current)" @click="makeBase(current)">以 v{{ current.version }} 开始修改</button>
          <a v-if="previewUrl(current)" class="ui-button quiet sm" :href="previewUrl(current)" :download="current.filename"><Download :size="14" />下载当前图片</a>
          <button v-if="compact && versions.length && !historyOpen && !comparison" class="ui-button quiet sm wb-version-toggle" @click="versionsExpanded = !versionsExpanded">{{ versionsExpanded ? '收起版本' : '查看版本（' + versions.length + '）' }}</button>
        </div>
        <!-- 版本芯片条与「全部版本」入口在比较视图下让位给图片池：同一批版本不该出现两处。
             「采用此版本／下载当前图片」这一行动作仍然保留，它们作用于当前查看的版本。 -->
        <div v-if="versions.length && !comparison && (!compact || versionsExpanded || historyOpen)" class="wb-version-strip" aria-label="图片版本">
          <button v-for="image in strip" :key="key(image)" :class="{ current: same(current, image), adopted: adopted === image.version }" @click="viewVersion(image)">
            <img v-if="previewUrl(image) && !previewErrors[image.artifact_id]" :src="previewUrl(image)" :alt="'v' + image.version" loading="lazy" @error="decodeError(image)" />
            <ImageOff v-else :size="18" /><strong>v{{ image.version }}</strong>
            <small>{{ unavailable(image) ? (adopted === image.version ? '原采用版本 · 不可用' : '不可用') : adopted === image.version ? '已采用' : image.parent ? '来自 v' + image.parent.version : '原始导入' }}</small>
          </button>
        </div>
        <div v-if="versions.length && !comparison && (!compact || versionsExpanded || historyOpen)" class="wb-actions">
          <button class="ui-button quiet sm" @click="historyOpen = !historyOpen">{{ historyOpen ? '收起历史' : '全部版本' }}</button>
          <button v-if="historyOpen && nextVersion" class="ui-button quiet sm" @click="loadVersions(current!.image_id, true)">加载更早版本</button>
        </div>
        <!-- 只在真的产出核验报告时占位。原先那句常驻提示与两个空槽里的引导语重复，
             白占一行高度，把它还给上面两个大图。 -->
        <div v-if="comparison && comparisonReport" class="wb-compare-foot">
          <div class="wb-report">
            <p>{{ comparisonReport.output.summary }}</p>
            <p v-for="check in [...comparisonReport.output.changes, ...comparisonReport.output.preservation]" :key="check.check_id">
              {{ check.status === 'satisfied' ? '已满足' : check.status === 'not_satisfied' ? '未满足' : '无法确定' }}：{{ check.evidence }}
            </p><small v-for="limitation in comparisonReport.output.limitations" :key="limitation">{{ limitation }}</small>
          </div>
        </div>
      </div>
      <aside ref="panelElement" class="wb-panel" :class="{ 'is-plain': comparison }">
        <template v-if="base && draft">
          <p v-if="unavailable(base)" class="workspace-error" role="status">修改基础 v{{ base.version }} 不可用。{{ unavailableReason(base) }} {{ recoveryHint(base) }} 原修改需求已保留。</p>
          <p v-if="operationErrors.history" class="workspace-error" role="status">{{ operationErrors.history }}</p>
          <div class="wb-panel-heading"><h4>{{ visibleInstruction && (historyInstruction || draft.phase === 'review') ? '核对改图指令' : '本轮修改' }}</h4></div>
          <nav v-if="!(visibleInstruction && (historyInstruction || draft.phase === 'review'))" class="wb-panel-tabs" aria-label="修改面板">
            <button :aria-pressed="panelTab === 'requirements'" @click="panelTab = 'requirements'; diagnosticsOpen = false">本轮需求（{{ modifications.length + (draft.instruction.trim() ? 1 : 0) }}）</button>
            <button :aria-pressed="panelTab === 'analysis'" @click="panelTab = 'analysis'; diagnosticsOpen = false">分析建议（{{ analysis?.output.findings.length ?? 0 }}）</button>
          </nav>
          <div class="wb-panel-scroll" :class="{ 'wb-editor': !(visibleInstruction && (historyInstruction || draft.phase === 'review')) }">
            <template v-if="visibleInstruction && (historyInstruction || draft.phase === 'review')">
              <small v-if="historyInstruction">历史指令，仅针对其原图片版本和当时需求。</small>
              <p>{{ visibleInstruction.scope.edit_scope?.mode === 'mixed' ? '修改范围：局部与整图分别授权' : visibleInstruction.scope.edit_scope?.mode === 'whole' ? '修改范围：整图' : visibleInstruction.scope.edit_scope ? '修改范围：已确认区域' : '历史指令未记录区域定位' }}</p>
              <pre class="wb-instruction">{{ finalImagePrompt(visibleInstruction.output) }}</pre>
              <details><summary>需要保留与指令依据</summary><p v-for="item in visibleInstruction.basis.constraints ?? []" :key="item.requirement">已确认要求：{{ item.requirement }}</p><p v-for="item in visibleInstruction.scope.protected_features" :key="item">建议保留：{{ item }}</p></details>
              <button v-if="!onBase" class="ui-button quiet sm" @click="locate('')">查看此指令原图</button>
              <small>将文字和原图一起交给外部工具；区域框提供定位，执行精度取决于工具能力。</small>
            </template>
            <template v-else>
              <div v-if="diagnosticsOpen" class="wb-diagnostics">
                <button class="ui-button quiet sm" @click="diagnosticsOpen = false"><ArrowLeft :size="14" />返回需求</button>
                <p class="workspace-error">{{ operationErrors.edit }}</p>
                <p>修改需求和选区已保留。以下是本次整理或核验未通过的原因。</p>
                <article v-for="(issue, index) in editDiagnostics?.issues ?? []" :key="index">
                  <p>{{ issue.reason }}</p>
                  <small v-if="issue.constraint_ids.length && !diagnosticTarget(issue)">涉及创作约束，不能通过扩大选区自动覆盖。</small>
                  <button v-if="diagnosticTarget(issue)" class="ui-button sm" @click="diagnosticsOpen = false; revealRequirement(diagnosticTarget(issue))">查看对应需求</button>
                </article>
                <small v-if="!editDiagnostics">此记录未提供具体条目，不能据此判断你的填写有误。可保留当前内容，再核对本次请求状态。</small>
              </div>
              <template v-else>
                <div v-show="panelTab === 'requirements'" class="wb-requirement-editor" :class="{ 'detail-only': shortPanel && !!expandedItem }">
                  <div class="wb-requirement-list" aria-label="本轮需求清单">
                    <div class="wb-actions wb-selection-heading"><small>选择一项查看范围和要求</small><button v-if="removedSelection" class="ui-button quiet sm" @click="undoRemoval"><Undo2 :size="14" />撤销删除</button></div>
                    <small v-if="!modifications.length">从分析建议加入需求，或在图片上框选。</small>
                    <article v-for="item in modifications" :key="item.id" :ref="el => setRequirementElement(item.id, el)" class="wb-modification" :class="{ 'is-pending': item.status === '待定位', 'is-current': expandedItem === item.id }" :data-requirement="item.id">
                      <div class="wb-modification-heading">
                        <button class="wb-card-title" :title="item.title" :aria-expanded="expandedItem === item.id" @click="selectRequirement(item.id)">{{ item.title }}</button>
                        <span class="wb-scope-tag">{{ item.status }}</span>
                        <button v-if="item.regions.length" class="ui-button quiet sm" :aria-label="'定位需求 ' + item.title" @click="selectRequirement(item.id); locate(item.regions[0]!.region_id)"><Maximize :size="14" /></button>
                        <button class="ui-button quiet sm" :aria-label="'移除需求 ' + item.title" @click="removeRegion(item.id)"><X :size="14" /></button>
                      </div>
                    </article>
                    <article class="wb-modification" :class="{ 'is-current': expandedItem === 'supplement', 'is-pending': invalidSupplement }" data-requirement="supplement">
                      <div class="wb-modification-heading">
                        <button class="wb-card-title" :aria-expanded="expandedItem === 'supplement'" @click="selectRequirement('supplement')">{{ draft.instruction.trim() ? '补充 · ' + draft.instruction : '添加本轮补充要求' }}</button>
                        <span class="wb-scope-tag">{{ !draft.instruction.trim() ? '未填写' : invalidSupplement ? '待定位' : instructionTarget.mode === 'whole' ? '整图' : '区域' }}</span>
                        <button v-if="draft.instruction.trim()" class="ui-button quiet sm" aria-label="移除补充要求" @click="draft.instruction = ''; edit()"><X :size="14" /></button>
                      </div>
                    </article>
                  </div>
                  <section v-if="activeModification || expandedItem === 'supplement'" class="wb-active-detail" aria-label="当前需求详情">
                    <div class="wb-detail-heading"><button class="ui-button quiet sm" @click="expandedItem = ''"><ArrowLeft :size="14" />{{ shortPanel ? '返回清单' : '收起详情' }}</button><span>{{ activeModification?.status ?? '补充要求' }}</span></div>
                    <div class="wb-detail-scroll">
                      <template v-if="expandedItem === 'supplement'">
                        <label>作用范围<select :value="supplementTargetValue" aria-label="补充要求作用范围" @change="setSupplementTarget(($event.target as HTMLSelectElement).value)"><option value="selected_regions">全部已选区域</option><option value="whole">整图</option><option v-for="region in regions" :key="region.region_id" :value="'region:' + region.region_id">{{ region.label || '人工选区' }}</option><option v-if="instructionTarget.mode === 'region' && !regions.some(region => region.region_id === instructionTarget.region_id)" :value="'region:' + instructionTarget.region_id">目标选区已删除，请重新选择</option></select></label>
                        <textarea v-model="draft.instruction" rows="4" maxlength="4000" aria-label="本轮补充要求" placeholder="写下本轮要改变什么，以及需要保持的内容。" @input="edit()" @blur="saveDraft" />
                        <small>与已选需求一起整理，仅作用于上方指定范围。</small>
                        <p v-if="invalidSupplement" class="workspace-error">目标选区不存在，请重新选择范围。</p>
                      </template>
                      <template v-else-if="activeModification">
                        <strong class="wb-detail-title" :title="activeModification.title">{{ activeModification.title }}</strong>
                        <template v-if="activeModification.findingId">
                          <label>修改范围<select :value="activeModification.status === '整图' ? 'whole' : 'regions'" :aria-label="'建议 ' + activeModification.title + ' 的修改范围'" @change="setFindingScope(activeModification.findingId, ($event.target as HTMLSelectElement).value === 'whole')"><option value="regions">指定区域</option><option value="whole">作为整图要求</option></select></label>
                          <p class="wb-clamp" :title="activeModification.finding?.suggested_change">{{ activeModification.finding?.suggested_change }}</p>
                          <div v-if="activeModification.status === '待定位'" class="wb-binding-actions">
                            <button class="ui-button sm" @click="startDrawingFor(activeModification.findingId)"><Maximize :size="14" />框选绑定</button>
                            <select value="" :aria-label="'绑定建议 ' + activeModification.title + ' 到已有选区'" @change="bindExistingRegion(activeModification.findingId, ($event.target as HTMLSelectElement).value)"><option value="">绑定已有选区</option><option v-for="region in regions.filter(region => region.finding_id !== activeModification!.findingId)" :key="region.region_id" :value="region.region_id">{{ region.label || '人工选区' }}</option></select>
                          </div>
                          <small v-if="activeModification.status === '待定位'">先绑定区域，或明确作为整图要求，再整理指令。</small>
                        </template>
                        <div v-for="region in activeModification.regions" :key="region.region_id" class="wb-region-row">
                          <div class="wb-actions"><strong>{{ region.label || '人工选区' }}</strong><button class="ui-button quiet sm" @click="locate(region.region_id)">定位</button><button class="ui-button quiet sm" @click="removeRegion(region.region_id)">删除</button></div>
                          <select v-if="!activeModification.findingId && analysis" :value="region.finding_id ?? ''" aria-label="关联图片建议" @change="linkRegion(region, ($event.target as HTMLSelectElement).value)"><option value="">人工选区</option><option v-for="(finding, number) in analysis.output.findings" :key="finding.finding_id" :value="finding.finding_id">建议 {{ number + 1 }} · {{ finding.observation }}</option></select>
                          <label>区域修改要求<textarea v-model="region.instruction" rows="3" maxlength="1000" :aria-label="(region.label || '人工选区') + '的修改要求'" placeholder="写明这个区域要改变的内容；已加入建议也可直接按建议整理。" @input="edit()" /></label>
                        </div>
                        <details v-if="activeModification.finding"><summary>分析全文与重新绑定</summary><p>{{ activeModification.finding.observation }}</p><p>{{ activeModification.finding.suggested_change }}</p><small>{{ activeModification.finding.uncertainty }}</small><div v-if="activeModification.findingId && activeModification.status === '区域'" class="wb-binding-actions"><button class="ui-button sm" @click="startDrawingFor(activeModification.findingId)">重新框选</button><select value="" aria-label="重新绑定已有区域" @change="bindExistingRegion(activeModification.findingId, ($event.target as HTMLSelectElement).value)"><option value="">绑定已有选区</option><option v-for="region in regions.filter(region => region.finding_id !== activeModification!.findingId)" :key="region.region_id" :value="region.region_id">{{ region.label || '人工选区' }}</option></select></div></details>
                      </template>
                    </div>
                  </section>
                </div>
                <div v-show="panelTab === 'analysis'" class="wb-analysis-page">
                  <details><summary>本轮图片任务</summary><textarea v-model="draft.task" rows="2" maxlength="4000" @input="updateTask" /></details>
                  <button class="ui-button sm" :disabled="!!operation || organizing || !ready[base.artifact_id]" @click="analyze()"><Sparkles :size="14" />{{ operation === 'analysis' ? '正在分析…' : analysis ? '重新分析图片' : '分析图片' }}</button>
                  <p v-if="operationErrors.analysis" class="workspace-error" role="alert" data-operation="analysis">{{ operationErrors.analysis }}<small>仍可手动框选并填写修改需求。</small></p>
                  <small v-if="!analysis">可先分析图片，也可直接框选并填写修改需求。</small>
                  <template v-if="analysis">
                    <section class="wb-analysis-result" aria-label="图片分析结果" role="status">
                      <strong>{{ analysis.output.findings.length ? `分析完成 · ${analysis.output.findings.length} 条建议` : '分析完成 · 未发现明确偏差' }}</strong>
                      <p class="wb-summary">{{ analysis.output.summary }}</p>
                      <small v-if="!analysis.output.findings.length">本次没有需要加入的修改建议。你仍可直接框选，或填写自己的修改要求。</small>
                    </section>
                    <button class="ui-button quiet sm" @click="useManualInput">仅按我的需求修改</button>
                    <div class="wb-findings">
                      <div v-for="(finding, index) in analysis.output.findings" :key="finding.finding_id" class="wb-finding" :class="{ 'is-current': focus === 'finding:' + finding.finding_id }">
                        <label><input type="checkbox" :aria-label="'加入建议 ' + (index + 1)" :checked="draft.selected_finding_ids.includes(finding.finding_id)" :disabled="operation === 'analysis'" @change="toggleFinding(finding.finding_id)" /><span><strong class="wb-clamp">{{ index + 1 }}. {{ finding.observation }}</strong></span></label>
                        <button v-if="finding.region" class="ui-button quiet sm" @click="locate(regions.find(item => item.finding_id === finding.finding_id)?.region_id ?? 'finding:' + finding.finding_id)">定位</button><small v-else>待定位</small>
                        <details class="wb-finding-detail"><summary>查看分析全文</summary><p>{{ finding.observation }}</p><p>{{ finding.suggested_change }}</p><small v-if="finding.uncertainty">{{ finding.uncertainty }}</small><small v-if="draft.dismissed_finding_region_ids?.includes(finding.finding_id)">已移出本轮，重新勾选可加入。</small></details>
                      </div>
                    </div>
                    <details><summary>建议保留与分析限制</summary><p v-for="item in analysis.output.protected_features" :key="item">{{ item }}</p><small v-for="item in analysis.output.limitations" :key="item">{{ item }}</small></details>
                  </template>
                  <details v-if="instructions.length" :open="instructionHistory" @toggle="instructionHistory = ($event.target as HTMLDetailsElement).open"><summary>修改指令历史（{{ instructions.length }}）</summary><button v-for="instruction in instructions" :key="instruction.artifact_id" class="wb-history-item" @click="historyInstruction = instruction">{{ instruction.output.summary }}<small>{{ instruction.requires_confirmation ? '待确认' : '已确认' }}</small></button><button v-if="reportCursor" class="ui-button quiet sm" @click="moreInstructions">加载更早指令</button></details>
                </div>
              </template>
            </template>
            <div v-if="remoteDraft" class="wb-report"><strong>服务端草稿</strong><p>{{ remoteDraft.state?.instruction || '尚未填写需求' }}</p><small>选中 {{ remoteDraft.state?.selected_finding_ids.length ?? 0 }} 条建议，{{ remoteDraft.state?.edit_scope?.regions.length ?? 0 }} 个选区</small><button class="ui-button sm" @click="useRemote">采用服务端草稿</button><button class="ui-button sm" @click="keepLocal">保留本页内容并保存</button></div>
          </div>
          <footer class="wb-panel-footer">
            <template v-if="visibleInstruction && (historyInstruction || draft.phase === 'review')">
              <button class="ui-button primary" :disabled="!!operation" @click="confirmAndCopy"><Copy :size="14" />{{ visibleInstruction.requires_confirmation ? '确认并复制指令' : '复制已确认指令' }}</button>
              <div class="wb-actions"><a v-if="previewUrl(base)" class="ui-button sm" :href="previewUrl(base)" :download="base.filename"><Download :size="14" />下载原图</a><button class="ui-button quiet sm" @click="historyInstruction = null; draft.phase = 'editing'; saveDraft()">返回修改需求</button><button class="ui-button quiet sm" :disabled="!!operation" @click="makeBase(base, true)"><Plus :size="14" />开始新一轮</button></div>
            </template>
            <template v-else>
              <small class="wb-scope-summary" role="status">{{ scopeSummary }}</small>
              <button class="ui-button primary" :disabled="!!operation || organizing || checkingRequest || !ready[base.artifact_id] || (!draft.instruction.trim() && !draft.selected_finding_ids.length && !regions.some(item => item.instruction.trim()))" @click="organize">{{ checkingRequest ? '正在核对原请求…' : pendingEdit?.uncertain ? '核对原请求状态' : operation === 'edit' ? '正在整理…' : organizing ? '正在保存修改需求…' : '整理改图指令' }}</button>
              <button v-if="operationErrors.edit" class="wb-error-summary" role="alert" data-operation="edit" :title="operationErrors.edit" @click="diagnosticsOpen = true">{{ operationErrors.edit }} · 查看原因</button>
            </template>
            <small role="status">{{ saveState }}</small>
            <small v-if="feedback" class="wb-feedback" role="status">{{ feedback }}</small>
            <details v-if="saveError" class="workspace-error"><summary>{{ saveError }}</summary><button class="ui-button quiet sm" @click="saveDraft">重试保存</button><button class="ui-button quiet sm" @click="reloadDraft">读取服务端草稿核对</button></details>
          </footer>
        </template>
        <div v-else class="wb-empty"><p>导入图片后，选择修改范围和填写需求。</p></div>
      </aside>
    </div>
  </section>
</template>

<style scoped>
/* 圆角与投影直接贴在图片表面，随图片移动与缩放；工作区没有独立相框。 */
.image-workbench { display:flex; flex:1; flex-direction:column; min-height:0; width:100%;
  --wb-radius-image:12px;
  --wb-img-shadow:0 0 0 1px rgba(20,24,31,.07), 0 2px 10px rgba(20,24,31,.18), 0 1px 2px rgba(20,24,31,.10); }
.wb-viewbar, .wb-panel-heading { display: flex; gap: 12px; align-items: center; justify-content: space-between; flex-wrap: wrap; }
.wb-panel h4 { margin:0; }
.wb-layout { display:grid; flex:1; min-height:0; grid-template-columns:minmax(0,1fr) 380px; gap:var(--space-5); align-items:stretch; }
.workspace-error { color: var(--danger); overflow-wrap: anywhere; }
.wb-panel .workspace-error small { display: block; margin-top: 6px; color: var(--text-secondary); }
.wb-main, .wb-panel { min-width:0; min-height:0; }.wb-main { position:relative; display:flex; flex-direction:column; overflow:hidden; }.wb-viewbar { margin-bottom:var(--space-3); font-size:var(--font-meta); flex-shrink:0; }
/* 视图栏成为工作台唯一的图片工具栏：左侧是"看什么"，右侧是"怎么处理"，压缩掉原先独立的提示栏。 */
.wb-viewbar-left { display:flex; align-items:center; gap:var(--space-2); min-width:0; }
.wb-viewbar-left > span { color:var(--text-secondary); font-variant-numeric:tabular-nums; }
.wb-auto-pill { display:inline-flex; align-items:center; gap:6px; height:var(--control-sm); padding:0 var(--space-3); border:1px solid var(--border-strong); border-radius:var(--radius-pill); background:var(--surface); color:var(--text-secondary); font:inherit; font-size:var(--font-control); white-space:nowrap; cursor:pointer; transition:background var(--motion-fast), border-color var(--motion-fast), color var(--motion-fast); }
.wb-auto-pill:hover { border-color:var(--border-hover); color:var(--text-primary); }
.wb-auto-pill::before { content:''; width:6px; height:6px; border-radius:50%; background:var(--border-strong); transition:background var(--motion-fast); }
.wb-auto-pill[aria-pressed='true'] { border-color:var(--accent); background:var(--accent-soft); color:var(--accent); }
.wb-auto-pill[aria-pressed='true']::before { background:var(--accent); }
.wb-import { display:flex; flex:1; flex-direction:column; min-height:260px; overflow:hidden; border:0; padding:0; }.wb-import :deep(.image-import-controls) { flex-shrink:0; order:-1; margin:0 !important; padding:0 0 var(--space-2); }
/* 删除按钮跟在导入提示后面，靠右收口；常态安静，悬停才显红。 */
.wb-import-delete { margin-left:auto; }
.wb-import-delete:hover:not(:disabled) { border-color:var(--danger-border); background:var(--danger-soft); color:var(--danger); }
.wb-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.wb-canvas { position:relative; display:grid; flex:1; min-height:220px; place-items:center; overflow:hidden; touch-action:none;
  cursor:grab; }
.wb-viewport { position:absolute; overflow:hidden;
  border-radius:var(--wb-radius-image); box-shadow:var(--wb-img-shadow); }
.wb-image-bounds { position:absolute; line-height:0; touch-action:none; }
.wb-image-bounds > img { display:block; width:100%; height:100%; max-width:none; user-select:none; }
/* 浮在图片上的控件回到浅色实体卡：画布不再是暗底，玻璃拟态会失去依托。 */
.wb-view-tools { position:absolute; left:50%; bottom:var(--space-3); transform:translateX(-50%); z-index:3; display:flex; align-items:center; gap:2px; white-space:nowrap; padding:3px;
  background:var(--surface); border:1px solid var(--border-muted); border-radius:var(--radius-pill); box-shadow:var(--shadow-pop); font-size:var(--font-kicker); color:var(--text-secondary); }
.wb-view-tools .ui-button { border-color:transparent; background:transparent; color:var(--text-secondary); }
.wb-view-tools .ui-button:hover:not(:disabled) { background:var(--accent-soft); color:var(--accent); border-color:transparent; }
.wb-view-tools .ui-button[aria-pressed=true] { color:var(--accent); background:var(--accent-soft); }
/* 百分比同时是"回到 100%"的按钮：缩放跑偏后不必反复点加减。 */
.wb-zoom-reset { min-width:46px; height:var(--control-sm); padding:0 var(--space-2); border:0; border-radius:var(--radius-pill); background:transparent; color:var(--text-secondary); font:inherit; font-size:var(--font-kicker); font-variant-numeric:tabular-nums; cursor:pointer; transition:background var(--motion-fast), color var(--motion-fast); }
.wb-zoom-reset:hover { background:var(--accent-soft); color:var(--accent); }
.is-panning, .is-panning .wb-region { cursor:grab; }
.is-dragging, .is-dragging .wb-region { cursor:grabbing; }
/* 框选提示不压在画面上：跟在视图栏下方做一行状态，只在框选阶段出现，占位最小。 */
.wb-draw-status { display:flex; align-items:center; gap:6px; align-self:flex-start; flex-shrink:0; margin:0 0 var(--space-2);
  padding:4px 10px; border:1px solid color-mix(in srgb, var(--accent) 24%, transparent); border-radius:var(--radius-pill);
  background:var(--accent-soft); color:var(--accent); font-size:var(--font-kicker); }
.wb-overlay { position: absolute; inset: 0; pointer-events: none; }
.wb-region { position: absolute; border: 1px dashed var(--accent); border-radius:var(--radius-tile); box-shadow:0 0 0 1px rgba(255,255,255,.55); pointer-events: auto; cursor: pointer; }
.wb-region.selected { border: 2px solid var(--accent); background: transparent; box-shadow:0 0 0 1px rgba(255,255,255,.65); cursor: move; }
.wb-region.focused { outline: 3px solid color-mix(in srgb, var(--accent) 40%, transparent); outline-offset: 3px; }
/* 角标三个角收 4px、左下保持直角：让它"坐"进框角，而不是浮在画面上。 */
.wb-region-number { position: absolute; left: -1px; top: -22px; background: var(--accent); color: white; padding: 3px 6px; line-height: 16px; font-size: 12px;
  border-radius: var(--radius-tile) var(--radius-tile) var(--radius-tile) 0; }
.wb-region-delete { position:absolute; top:4px; right:4px; display:grid; place-items:center; width:28px; height:28px;
  border:1px solid var(--border-muted); border-radius:var(--radius-pill); background:var(--surface); color:var(--text-secondary); cursor:pointer;
  box-shadow:var(--shadow-card); transition:background var(--motion-fast), color var(--motion-fast), border-color var(--motion-fast); }
.wb-region-delete:hover { border-color:var(--danger); color:var(--danger); }
.wb-handle { position: absolute; width: 12px; height: 12px; padding: 0; background: white; border: 1px solid var(--accent); border-radius: var(--radius-tile); box-shadow:0 1px 3px rgba(0,0,0,.35); }
/* 手柄视觉 12px，命中区用 ::before 扩到 22px，鼠标和触摸都不必瞄得很准。 */
.wb-handle::before { content:''; position:absolute; inset:-5px; }
.nw { top: -6px; left: -6px; cursor: nwse-resize; }.ne { top: -6px; right: -6px; cursor: nesw-resize; }.sw { bottom: -6px; left: -6px; cursor: nesw-resize; }.se { bottom: -6px; right: -6px; cursor: nwse-resize; }
.is-drawing { cursor: crosshair; }
.wb-empty { min-height: 240px; display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 12px; padding: 20px; text-align: center; }
/* 右侧面板由"硬分割线"改为一张软卡：与画布灯箱同级，但回到浅色，避免整列转暗与外壳割裂。 */
.wb-panel { border:1px solid var(--border-muted); border-radius:var(--radius-panel); background:var(--surface-subtle); padding:var(--space-4); display:flex; flex-direction:column; gap:var(--space-3); overflow:hidden; }
.wb-panel-heading { flex-shrink:0; }
.wb-panel-scroll { display:flex; flex:1; flex-direction:column; gap:var(--space-3); min-height:0; overflow-y:auto; overscroll-behavior:contain; padding-right:var(--space-2); scrollbar-gutter:stable; }
.wb-panel-scroll > * { flex-shrink:0; }
.wb-panel-footer { display:flex; flex-direction:column; gap:var(--space-1); border-top:1px solid var(--border-muted); padding-top:var(--space-3); flex-shrink:0; }
.wb-panel-footer > .ui-button { width:100%; }
.wb-panel-footer p { margin:0; font-size:var(--font-meta); }
.wb-scope-summary { color:var(--text-secondary); }
.wb-panel label { display: flex; gap: 8px; align-items: flex-start; }.wb-panel input[type=checkbox], .wb-panel input[type=radio] { margin-top: 3px; }
.wb-panel textarea, .wb-panel input:not([type]), .wb-panel select, .wb-main select { width: 100%; min-width: 0; border: 1px solid var(--border-strong); border-radius: var(--radius-control); background: var(--surface); color: var(--text-primary); font: inherit; font-size: 13px; padding: 9px; }
.wb-panel textarea { resize:none; }.wb-panel details p { font-size:var(--font-meta); }.wb-panel details small { display:block; margin-top:var(--space-1); }
.wb-main select { width:auto; max-width:320px; }.wb-findings { display:flex; flex-direction:column; gap:var(--space-3); padding-top:var(--space-3); }
.wb-finding { display:flex; flex-wrap:wrap; gap:var(--space-1); align-items:start; }.wb-finding label { flex:1; }.wb-finding span { min-width:0; }.wb-finding-detail { flex-basis:100%; padding-left:24px; }
.wb-clamp { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.wb-finding strong, .wb-summary { font-size: 13px; line-height: 1.6; }.wb-finding small { display: block; margin-top: 5px; }
.wb-analysis-result { display:grid; gap:var(--space-2); font-size:var(--font-meta); }
.wb-analysis-result p { margin:0; }.wb-analysis-result small { color:var(--text-secondary); line-height:1.6; }
.wb-selection-heading { justify-content:space-between; font-size:var(--font-meta); }
/* 需求清单由"一条条横线"改为软卡：当前项用左侧强调条定位，不再靠底色区分。 */
.wb-modification { border:1px solid transparent; border-radius:var(--radius-control); padding:var(--space-2) var(--space-3); transition:background var(--motion-fast), border-color var(--motion-fast), box-shadow var(--motion-fast); }
.wb-modification:hover { background:var(--surface); border-color:var(--border-muted); }
.wb-modification-heading { display:flex; align-items:center; gap:var(--space-2); }
.wb-card-title { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; flex:1; min-width:0; overflow:hidden; border:0; padding:0; background:transparent; color:var(--text-primary); font:inherit; font-size:var(--font-meta); font-weight:600; text-align:left; cursor:pointer; }
.wb-modification.is-current { background:var(--surface); border-color:var(--border-muted); box-shadow:inset 3px 0 0 var(--accent), var(--shadow-card); }.wb-detail-title { font-size:var(--font-meta); display:-webkit-box; -webkit-line-clamp:1; -webkit-box-orient:vertical; overflow:hidden; }
/* 页签改成互斥分段控件（segmented）：贴合"两种看法"的语义，比下划线更像一个开关。 */
.wb-panel-tabs { display:flex; flex-shrink:0; gap:var(--space-1); padding:2px; border-radius:var(--radius-control); background:var(--canvas-inset); }
.wb-panel-tabs button { flex:1; border:0; border-radius:calc(var(--radius-control) - 2px); padding:var(--space-2) var(--space-1); background:none; color:var(--text-secondary); font:inherit; font-size:var(--font-meta); cursor:pointer; transition:background var(--motion-fast), color var(--motion-fast); }
.wb-panel-tabs button:hover { color:var(--text-primary); }
.wb-panel-tabs button[aria-pressed=true] { color:var(--accent); background:var(--surface); box-shadow:var(--shadow-card); }
.wb-editor { overflow:hidden; }.wb-editor > .wb-requirement-editor { flex:1; min-height:0; flex-shrink:1; }
.wb-requirement-editor { display:flex; flex-direction:column; overflow:hidden; }
.wb-requirement-list { flex:1; min-height:0; overflow-y:auto; overscroll-behavior:contain; display:flex; flex-direction:column; gap:2px; }
.wb-requirement-list > * { flex-shrink:0; }
.wb-active-detail { display:flex; flex-direction:column; flex:0 1 48%; min-height:140px; margin-top:var(--space-2); border-top:1px solid var(--border-strong); padding-top:var(--space-2); }
.wb-detail-heading { display:flex; align-items:center; justify-content:space-between; flex-shrink:0; font-size:var(--font-kicker); color:var(--text-secondary); }
.wb-detail-scroll { overflow-y:auto; min-height:0; display:flex; flex-direction:column; gap:var(--space-2); padding-top:var(--space-2); }
.wb-detail-scroll > * { flex-shrink:0; }.wb-detail-scroll p { margin:0; font-size:var(--font-meta); line-height:1.6; }
.wb-detail-scroll label { flex-direction:column; }.wb-detail-scroll > label { flex-direction:row; align-items:center; }.wb-detail-scroll > label select { flex:1; }.detail-only .wb-requirement-list { display:none; }.detail-only .wb-active-detail { flex:1; margin:0; border:0; padding:0; }
.wb-analysis-page, .wb-diagnostics { overflow-y:auto; min-height:0; display:flex; flex:1; flex-shrink:1 !important; flex-direction:column; gap:var(--space-3); }
.wb-analysis-page > *, .wb-diagnostics > * { flex-shrink:0; }.wb-diagnostics { font-size:var(--font-meta); }
.wb-error-summary { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; text-align:left; color:var(--danger); border:0; padding:0; background:none; font:inherit; font-size:var(--font-meta); cursor:pointer; }
.wb-version-toggle { align-self:flex-start; }.is-compact .wb-import :deep(.image-import-controls small) { display:none; }
.wb-scope-tag { flex-shrink:0; font-size:var(--font-kicker); color:var(--text-secondary); }
.is-pending .wb-scope-tag { color:var(--warning); }
.wb-binding-actions { display:flex; gap:var(--space-2); }.wb-binding-actions select { flex:1; min-width:0; }
.wb-region-row { display: flex; flex-direction: column; gap: 8px; border-bottom: 1px solid var(--border-muted); padding-bottom: 12px; font-size: 13px; }
.wb-import-confirm, .wb-report { border: 1px solid var(--border-muted); border-radius: var(--radius-control); padding: 16px; margin-top: 12px; display: flex; flex-direction: column; gap: 10px; font-size: 13px; }
/* 导入确认由"悬浮在内容上方的一块板"改为锚定画布底部的 popover：贴近触发它的控件，也更像一次性决定。 */
.wb-import-confirm { position:absolute; left:var(--space-3); right:var(--space-3); bottom:var(--space-3); z-index:4; max-height:62%; overflow-y:auto;
  background:var(--surface); border-color:var(--border); border-radius:var(--radius-panel); box-shadow:var(--shadow-pop); }
.wb-image-actions { margin:var(--space-1) 0; flex-shrink:0; }
/* 版本条是浅色控制带，不进暗区；缩略图给中性底，透明 PNG 在浅底上也能看清边界。 */
.wb-version-strip { display:flex; gap:var(--space-2); overflow-x:auto; padding:4px 0 8px; flex-shrink:0; }
.wb-version-strip button { position:relative; flex:0 0 auto; display:flex; gap:6px; align-items:center; padding:5px 8px; background:var(--surface); color:var(--text-primary); border:1px solid var(--border-muted); border-radius:var(--radius-dialog);
  font-size:var(--font-meta); transition:border-color var(--motion-fast), box-shadow var(--motion-fast), transform var(--motion-fast) var(--ease-out); }
.wb-version-strip button:hover { transform:translateY(-1px); box-shadow:var(--shadow-card); }
.wb-version-strip button.current { border-color:var(--accent); box-shadow:0 0 0 2px var(--accent-ring); }
.wb-version-strip button.adopted::after { content:''; position:absolute; top:3px; right:3px; width:7px; height:7px; border-radius:50%; background:var(--success); box-shadow:0 0 0 2px var(--surface); }
.wb-version-strip img { height:44px; width:48px; object-fit:contain; border-radius:var(--radius-tile); background:var(--canvas-inset); }
.wb-version-strip strong { font-variant-numeric:tabular-nums; }
.wb-version-strip small { font-size: 11px; }
/* —— 版本比较视图：上方两张对比大图 + 底部图片池（点选或拖拽选图） —— */
.wb-compare { display:flex; flex:1; min-height:0; flex-direction:column; gap:var(--space-3); }
.wb-compare-stage { flex:1; min-height:0; display:grid; grid-template-columns:1fr 1fr; gap:var(--space-4); }
.wb-compare-slot { display:flex; flex-direction:column; min-width:0; min-height:0; overflow:hidden;
  border:1px solid var(--border-muted); border-radius:var(--radius-panel); background:var(--surface);
  transition:border-color var(--motion-fast), box-shadow var(--motion-fast); }
/* 拖进来时整张框亮起：比"换一圈虚线"更能说明"松手就放这里"。 */
.wb-compare-slot.is-drop { border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-ring); }
.wb-compare-head { display:flex; align-items:center; gap:var(--space-2); padding:var(--space-2) var(--space-3);
  border-bottom:1px solid var(--border-muted); background:var(--surface-subtle); }
.wb-compare-head strong { font-size:var(--font-control); font-weight:600; white-space:nowrap; }
.wb-compare-head small { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--text-muted); font-size:var(--font-kicker); }
.wb-compare-tag { display:grid; place-items:center; flex-shrink:0; width:20px; height:20px; border-radius:var(--radius-tile);
  background:var(--accent-soft); color:var(--accent); font-size:11px; font-weight:600; }
.wb-compare-clear { display:grid; place-items:center; flex-shrink:0; width:24px; height:24px; margin-left:auto;
  border:1px solid transparent; border-radius:var(--radius-pill); background:transparent; color:var(--text-muted); cursor:pointer;
  transition:border-color var(--motion-fast), color var(--motion-fast); }
.wb-compare-clear:hover { border-color:var(--danger); color:var(--danger); }
/* 图片区退回中性底：浅色图和深色图都能看清边缘。
   图片必须在"确定的盒子"里等比缩放，否则会被裁成放大的局部 —— 踩过两个坑：
   ① 当 grid item 时，隐式行按内容撑开（绝对 808px 高），height:100% 解析不出来；
   ② 绝对定位后只写 inset 也不行：<img> 是替换元素，width/height 为 auto 时用的
      是内在尺寸（508×859），inset 只定住左上角，图片照样按原尺寸溢出。
   所以这里显式给 width/height:100% + padding 当内缩，让 object-fit 有真正的容器。 */
.wb-compare-body { position:relative; flex:1; min-height:0; background:var(--canvas-inset); }
.wb-compare-image { position:absolute; inset:0; box-sizing:border-box; padding:var(--space-3);
  width:100%; height:100%; object-fit:contain; }
/* 空槽不只是一圈边框：淡点阵底 + 图标胶囊 + 标题 + 可照做的提示。 */
.wb-compare-placeholder { position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center;
  gap:var(--space-2); padding:var(--space-4); text-align:center; background-color:var(--canvas-inset);
  background-image:radial-gradient(circle at 50% 50%, rgba(255,255,255,.94), rgba(255,255,255,0) 72%),
    radial-gradient(circle, rgba(20,24,31,.09) 1px, transparent 1px);
  background-size:100% 100%, 16px 16px; transition:background-color var(--motion-fast); }
.wb-compare-slot.is-drop .wb-compare-placeholder { background-color:var(--accent-soft); }
.wb-compare-placeholder-icon { display:grid; place-items:center; width:46px; height:46px; border-radius:14px;
  background:var(--accent-soft); color:var(--accent); box-shadow:0 0 0 1px rgba(66,92,199,.14); }
.wb-compare-placeholder strong { font-size:var(--font-control); font-weight:600; }
.wb-compare-placeholder small { max-width:26ch; color:var(--text-muted); line-height:1.5; }
/* 底部图片池：横向条，可横向滚动查看更早版本。 */
.wb-compare-pool { flex-shrink:0; display:flex; flex-direction:column; gap:var(--space-2); padding:var(--space-3);
  border:1px solid var(--border-muted); border-radius:var(--radius-panel); background:var(--surface-subtle); }
.wb-compare-pool-head { display:flex; align-items:center; gap:var(--space-2); flex-wrap:wrap; }
.wb-compare-pool-head strong { font-size:var(--font-control); font-weight:600; white-space:nowrap; }
.wb-compare-pool-head small { min-width:0; color:var(--text-muted); font-size:var(--font-kicker); }
.wb-compare-pool-head .ui-button { margin-left:auto; }
.wb-compare-pool-list { display:flex; gap:var(--space-2); overflow-x:auto; padding-bottom:2px; overscroll-behavior-x:contain; }
/* 缩略图用横向排布：一条 40px 高的竖列会把两个对比框挤矮，横排只占一半高度。 */
.wb-compare-thumb { display:flex; align-items:center; flex:0 0 auto; gap:8px; padding:4px 8px 4px 4px; text-align:left;
  border:1px solid var(--border-muted); border-radius:var(--radius-control); background:var(--surface); cursor:grab;
  transition:border-color var(--motion-fast), box-shadow var(--motion-fast); }
.wb-compare-thumb:hover { border-color:var(--border-hover); box-shadow:var(--shadow-card); }
.wb-compare-thumb.is-used { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent); cursor:default; }
.wb-compare-thumb-img { display:grid; place-items:center; flex-shrink:0; width:40px; height:40px; overflow:hidden;
  border-radius:var(--radius-tile); background:var(--canvas-inset); color:var(--text-muted); }
.wb-compare-thumb-img img { width:100%; height:100%; object-fit:cover; }
.wb-compare-thumb-text { display:flex; flex-direction:column; gap:1px; min-width:0; }
.wb-compare-thumb-text strong { font-size:var(--font-kicker); font-weight:600; }
.wb-compare-thumb-text small { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--text-muted); font-size:11px; }
.wb-compare-foot { display:flex; flex-direction:column; gap:var(--space-2); align-items:flex-start; flex-shrink:0; max-height:30%; overflow-y:auto; }
.wb-compare-foot small { color:var(--text-muted); font-size:var(--font-meta); }
/* 比较视图下右侧面板去掉边框与底色：让两张对比图成为唯一焦点。 */
.wb-panel.is-plain { border-color:transparent; background:transparent; }
.wb-instruction { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 52vh; overflow-y: auto; font: inherit; font-size: 13px; line-height: 1.7; margin: 0; padding: 12px; background: var(--surface-subtle, #edf0f2); border-radius: var(--radius-control); }
.wb-history-item { display: block; width: 100%; text-align: left; margin-top: 8px; padding: 10px; border: 1px solid var(--border-muted); border-radius: var(--radius-control); color: var(--text-primary); background: var(--surface); }
.wb-feedback { font-size: 13px; color: var(--accent); }small { color: var(--text-muted); line-height: 1.6; }
@container image-stage (max-width: 959px) {
  .image-workbench { overflow-y:auto; overscroll-behavior:contain; }
  .wb-layout { display:flex; flex:none; flex-direction:column; }
  .wb-main { overflow:visible; flex-shrink:0; }
  .wb-import { flex:none; }.wb-canvas { height:clamp(260px,50vh,520px); flex:none; min-height:260px; }
  .wb-panel { height:min(80vh,760px); min-height:420px; }
  .wb-import-confirm { position:static; max-height:400px; }
  .wb-main select { max-width:220px; }
  /* 窄容器下两个对比槽改为纵向堆叠，各自给足固定高度；图片池依旧留在最底部。
     堆叠后槽位是整宽的，高度就不能再按原来的 42vh 给 —— 否则会变成又宽又扁的一条，
     竖构图素材只能占中间一小块。放宽到 50vh 与上面画布同量级，多出来的高度由页面滚动承接。 */
  .wb-compare { flex:none; }
  .wb-compare-stage { flex:none; grid-template-columns:1fr; }
  .wb-compare-slot { height:clamp(260px,50vh,480px); }
  .wb-compare-foot { max-height:none; }
}
@media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto; } }
</style>
