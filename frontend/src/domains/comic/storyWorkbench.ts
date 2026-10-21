import { computed, reactive } from 'vue'
import * as core from '../../services/core'
import type { ComicImageRef, ComicImageView, ComicStoryDraft, ComicStoryView, ComicStoryWrite } from '../../services/core'

const emptyDraft = (): ComicStoryDraft => ({ title: '', summary: '', instruction: '', shots: [] })
const cloneDraft = (draft: ComicStoryDraft): ComicStoryDraft => ({ title: draft.title, summary: draft.summary,
  instruction: draft.instruction, shots: draft.shots.map(shot => ({ ...shot, image: { image_id: shot.image.image_id, version: shot.image.version } })) })
export const storyImageKey = (image: ComicImageRef): string => `${image.image_id}:${image.version}`
/** 校验结果必须说清"改哪里"：字段中文名与错误类型都由这里统一。 */
export const storyFieldLabels: Record<string, string> = {
  title: '故事标题', summary: '故事概述', instruction: '本轮故事要求', action: '动作',
  camera: '运镜', duration_seconds: '时长', transition: '转场', image: '图片',
}
export const storyCodeLabels: Record<string, string> = {
  missing_title: '标题缺失', missing_summary: '概述缺失', missing_transition: '转场缺失',
  missing_shots: '缺少镜头', missing_action: '动作缺失', missing_camera: '运镜缺失',
  missing_duration: '时长缺失', duration_conflict: '时长不合理', camera_conflict: '运镜与动作冲突',
  transition_conflict: '镜头衔接不连贯', image_conflict: '图片与内容不符',
  requirement_conflict: '与用户要求冲突', continuity: '镜头不连贯', other: '其他问题',
}
type StoryApi = Pick<typeof core, 'getComicStory' | 'getComicStoryImages' | 'getComicStoryVersions' | 'getComicStoryRequests'
  | 'getComicProject' | 'saveComicStory' | 'generateComicStory' | 'reviewComicStory' | 'confirmComicStory'
  | 'restoreComicStory' | 'adoptComicStoryCandidate' | 'exportComicStory' | 'getArtifactContentUrl'>

/** UI session controller, owned by DirectorWorkspace. The server defines versions and approval.
 * Draft edits outlive the story/export view; a late request never replaces newer local edits. */
export function createStoryWorkbench(anchor: () => { project_id: string; current_version: number } | null, api: StoryApi = core) {
  const state = reactive({ owner: '', loaded: false, loading: false, saving: false, operation: '', dirty: false,
    conflict: false, error: '', notice: '', view: null as ComicStoryView | null, draft: emptyDraft(),
    images: [] as ComicImageView[], versions: [] as core.ComicStory[], selectedImages: [] as string[],
    imageWarnings: [] as (ComicImageRef & { reason: string })[],
    previews: {} as Record<string, string>, previewErrors: {} as Record<string, string>,
    unknownRequest: null as { request_id: string; action: string } | null, candidateRunId: '' })
  let epoch = 0, editEpoch = 0, loadSequence = 0, disposed = false
  let autosave: ReturnType<typeof setTimeout> | null = null
  let savePromise: Promise<boolean> | null = null
  const previewLoading = new Set<string>()
  let visiblePreviewIds = new Set<string>()
  const dirty = computed(() => state.dirty)
  const confirmed = computed(() => state.loaded && !state.dirty && !state.conflict && !!state.view?.confirmation?.confirmed)
  const canConfirm = computed(() => state.loaded && !state.dirty && !state.conflict && !!state.view?.review?.validation_requested
    && state.view.review.status !== 'needs_revision' && !state.view.review.findings.some(item => item.severity === 'error')
    && !state.view.missing_images.length)
  const canExport = computed(() => confirmed.value && !state.view?.missing_images.length && !!state.view?.export_text)
  const saveLabel = computed(() => state.loading ? '正在读取故事' : state.saving ? '正在保存' : state.conflict ? '版本冲突，修改已保留'
    : state.unknownRequest ? '正在核对请求结果' : state.dirty ? '有未保存修改' : state.view?.story ? `已保存 v${state.view.story.version}` : '尚未建立故事')
  const requestStorage = (owner: string) => `kantoku-story-request:${owner}`
  function rememberRequest(value: typeof state.unknownRequest): void {
    try { if (value) sessionStorage.setItem(requestStorage(state.owner), JSON.stringify(value)); else sessionStorage.removeItem(requestStorage(state.owner)) }
    catch { /* Request history remains queryable from the server when browser storage is unavailable. */ }
  }
  function clearPreviews(): void {
    Object.values(state.previews).forEach(url => URL.revokeObjectURL(url))
    state.previews = {}; state.previewErrors = {}
    previewLoading.clear()
    visiblePreviewIds.clear()
  }
  function applyView(view: ComicStoryView, replaceDraft: boolean): void {
    if ((view.story?.version ?? 0) < (state.view?.story?.version ?? 0)) return
    state.view = view
    if (view.story && !state.versions.some(item => item.version === view.story!.version)) state.versions.unshift(view.story)
    if (replaceDraft) { state.draft = view.story ? cloneDraft(view.story) : emptyDraft(); state.dirty = false; state.conflict = false }
    if (view.candidate_run_id) state.candidateRunId = view.candidate_run_id
  }
  function previewFailed(image: { artifact_id: string }): void {
    const url = state.previews[image.artifact_id]
    if (url) URL.revokeObjectURL(url)
    delete state.previews[image.artifact_id]
    state.previewErrors[image.artifact_id] = '图片无法显示，请重新读取原图。'
  }
  async function preview(image: { artifact_id: string; available?: boolean; unavailable_reason?: string | null }, retry = false): Promise<void> {
    const known = state.images.find(item => item.artifact_id === image.artifact_id)
    const frozen = state.view?.story?.fixed_images.find(item => item.artifact_id === image.artifact_id)
    const missing = frozen && state.view?.missing_images.find(item => item.image_id === frozen.image_id && item.image_version === frozen.version)
    if (image.available === false || known?.available === false || missing) {
      const old = state.previews[image.artifact_id]
      if (old) URL.revokeObjectURL(old)
      delete state.previews[image.artifact_id]
      state.previewErrors[image.artifact_id] = missing?.reason || image.unavailable_reason || known?.unavailable_reason || '图片已不可用，请选择有效素材。'
      return
    }
    if (retry) delete state.previewErrors[image.artifact_id]
    if (state.previews[image.artifact_id] || state.previewErrors[image.artifact_id] || previewLoading.has(image.artifact_id)) return
    previewLoading.add(image.artifact_id)
    const ownerEpoch = epoch
    try {
      const url = await api.getArtifactContentUrl(image.artifact_id, true)
      if (!url) throw new Error('图片文件暂不可读取')
      if (disposed || ownerEpoch !== epoch || !visiblePreviewIds.has(image.artifact_id)) { URL.revokeObjectURL(url); return }
      // A bounded preview cache; revisiting an evicted item fetches its original Artifact again.
      const oldest = Object.keys(state.previews).find(id => !visiblePreviewIds.has(id))
      if (Object.keys(state.previews).length >= 16 && oldest) { URL.revokeObjectURL(state.previews[oldest]!); delete state.previews[oldest] }
      state.previews[image.artifact_id] = url
    } catch (failure) {
      if (ownerEpoch === epoch) state.previewErrors[image.artifact_id] = failure instanceof Error ? failure.message : '图片读取失败'
    } finally { if (ownerEpoch === epoch) previewLoading.delete(image.artifact_id) }
  }
  async function showImages(images: { artifact_id: string }[]): Promise<void> {
    visiblePreviewIds = new Set(images.map(image => image.artifact_id))
    for (const [id, url] of Object.entries(state.previews)) {
      if (!visiblePreviewIds.has(id)) { URL.revokeObjectURL(url); delete state.previews[id] }
    }
    await Promise.all(images.map(image => preview(image)))
  }
  async function load(owner = state.owner): Promise<void> {
    if (!owner || disposed) return
    if (state.owner !== owner) {
      epoch++; editEpoch++; clearPreviews(); if (autosave) clearTimeout(autosave)
      Object.assign(state, { owner, loaded: false, dirty: false, conflict: false, error: '', notice: '', view: null,
        draft: emptyDraft(), images: [], imageWarnings: [], versions: [], selectedImages: [], operation: '', saving: false, unknownRequest: null, candidateRunId: '' })
      savePromise = null
      try { state.unknownRequest = JSON.parse(sessionStorage.getItem(requestStorage(owner)) ?? 'null') } catch { state.unknownRequest = null }
    }
    const ownerEpoch = epoch, sequence = ++loadSequence, edits = editEpoch
    state.loading = true; state.error = ''; state.notice = ''
    try {
      const [viewResult, imagesResult, versionsResult] = await Promise.allSettled([api.getComicStory(owner), api.getComicStoryImages(owner), api.getComicStoryVersions(owner)])
      if (ownerEpoch !== epoch || sequence !== loadSequence || disposed) return
      const view = viewResult.status === 'fulfilled' ? viewResult.value : null
      if (view && (view.story?.version ?? 0) >= (state.view?.story?.version ?? 0)) applyView(view, !state.dirty && edits === editEpoch)
      if (imagesResult.status === 'fulfilled' && imagesResult.value) {
        state.images = imagesResult.value.images
        state.imageWarnings = imagesResult.value.unavailable_images ?? []
        const usable = new Set(state.images.filter(image => image.available !== false).map(storyImageKey))
        state.selectedImages = state.selectedImages.filter(key => usable.has(key))
        state.images.filter(image => image.available === false).forEach(image => { void preview(image) })
      }
      if (versionsResult.status === 'fulfilled') state.versions = versionsResult.value
      state.loaded = !!view
      const failures = [viewResult, imagesResult, versionsResult].filter(result => result.status === 'rejected')
      if (failures.length) state.error = failures.map(result => result.reason instanceof Error ? result.reason.message : '部分故事资料读取失败').join('；')
      if (!visiblePreviewIds.size) await showImages([...state.images.slice(0, 8), ...(view?.story?.fixed_images ?? []).slice(0, 8)])
      if (ownerEpoch === epoch) await recoverRequest()
    } catch (failure) { if (ownerEpoch === epoch) state.error = failure instanceof Error ? failure.message : '故事读取失败' }
    finally { if (ownerEpoch === epoch && sequence === loadSequence) state.loading = false }
  }
  function changed(): void {
    editEpoch++; state.dirty = true; state.notice = ''
    if (autosave) clearTimeout(autosave)
    if (!state.conflict && !state.unknownRequest) autosave = setTimeout(() => { void save() }, 650)
  }
  function toggleImage(image: ComicImageRef): void {
    if (state.images.find(item => storyImageKey(item) === storyImageKey(image))?.available === false) return
    const key = storyImageKey(image), index = state.selectedImages.indexOf(key)
    if (index < 0) { if (state.selectedImages.length >= 8) { state.notice = 'AI 初稿每次最多选择 8 张图片。'; return }; state.selectedImages.push(key) }
    else state.selectedImages.splice(index, 1)
  }
  function addImage(image: ComicImageRef): void {
    if (state.images.find(item => storyImageKey(item) === storyImageKey(image))?.available === false) return
    if (state.draft.shots.length >= 100) { state.notice = '一个故事最多 100 个镜头。'; return }
    state.draft.shots.push({ shot_id: crypto.randomUUID(), image: { image_id: image.image_id, version: image.version }, action: '', camera: '', duration_seconds: null, transition: '' }); changed()
  }
  function moveShot(index: number, target: number): void {
    if (index === target || target < 0 || target >= state.draft.shots.length) return
    const [shot] = state.draft.shots.splice(index, 1)
    if (shot) { state.draft.shots.splice(target, 0, shot); changed() }
  }
  function removeShot(index: number): void { state.draft.shots.splice(index, 1); changed() }
  function replaceImage(index: number, key: string): void {
    const image = state.images.find(item => storyImageKey(item) === key), shot = state.draft.shots[index]
    if (image && image.available !== false && shot) { shot.image = { image_id: image.image_id, version: image.version }; changed() }
  }
  function imageReplacement(shot: core.ComicStoryShot): ComicImageView | undefined {
    if (!state.view?.missing_images.some(item => item.shot_id === shot.shot_id)) return
    const frozen = state.view.story?.fixed_images.find(item => storyImageKey(item) === storyImageKey(shot.image))
    if (!frozen) return
    return state.images.find(image => image.available !== false && !!image.sha256 && image.sha256 === frozen.sha256
      && image.mime_type === frozen.mime_type && image.project_id === frozen.project_id
      && image.conversation_id === frozen.conversation_id && storyImageKey(image) !== storyImageKey(frozen))
  }
  function repairImage(index: number): void {
    const shot = state.draft.shots[index], replacement = shot && imageReplacement(shot)
    if (replacement) replaceImage(index, storyImageKey(replacement))
  }
  async function writeBody(requestId: string): Promise<ComicStoryWrite> {
    const projectId = state.view?.anchor_project_id ?? state.view?.story?.anchor_project_id ?? anchor()?.project_id
    if (!projectId) throw new Error('请先建立作品或导入并采用图片，再开始编排故事。')
    const project = await api.getComicProject(projectId)
    if (!project) throw new Error('故事所属作品暂不可读取，请稍后重新保存。')
    return { conversation_id: state.owner, request_id: requestId, anchor_project_id: projectId,
      expected_project_version: project.project.current_version, expected_story_version: state.view?.story?.version ?? 0 }
  }
  async function save(allowConflict = false): Promise<boolean> {
    if (autosave) { clearTimeout(autosave); autosave = null }
    if (savePromise) return savePromise
    if (!state.dirty) return true
    if (!state.owner || state.unknownRequest || (state.conflict && !allowConflict)) return false
    const ownerEpoch = epoch, owner = state.owner
    const runSave = async (): Promise<boolean> => {
      state.saving = true; state.error = ''; if (allowConflict) state.conflict = false
      try {
        while (state.dirty && ownerEpoch === epoch && !disposed) {
          const edits = editEpoch, draft = cloneDraft(state.draft), requestId = crypto.randomUUID()
          const body = await writeBody(requestId)
          if (ownerEpoch !== epoch || disposed) return false
          rememberRequest({ request_id: requestId, action: 'save' })
          try {
            const view = await api.saveComicStory(owner, { ...body, draft })
            if (ownerEpoch !== epoch || disposed) return false
            applyView(view, edits === editEpoch); rememberRequest(null)
            state.dirty = edits !== editEpoch
          } catch (failure) {
            if (ownerEpoch !== epoch || disposed) return false
            const status = failure instanceof core.CoreApiError ? failure.httpStatus : 0
            if (status === 409) {
              state.conflict = true; rememberRequest(null)
              const latest = await api.getComicStory(owner); if (latest && ownerEpoch === epoch) applyView(latest, false)
            } else if (!status || status >= 500) { state.unknownRequest = { request_id: requestId, action: 'save' } }
            else rememberRequest(null)
            throw failure
          }
        }
        return !state.dirty
      } catch (failure) { if (ownerEpoch === epoch) state.error = failure instanceof Error ? failure.message : '保存没有完成'; return false }
      finally { if (ownerEpoch === epoch) state.saving = false }
    }
    savePromise = runSave()
    try { return await savePromise } finally { if (ownerEpoch === epoch) savePromise = null }
  }
  async function recoverRequest(): Promise<void> {
    const ownerEpoch = epoch, owner = state.owner
    if (!owner) return
    try {
      const tasks = await api.getComicStoryRequests(owner)
      if (ownerEpoch !== epoch || disposed) return
      const pending = state.unknownRequest
      const run = pending ? tasks.find(item => (item.state.media_input as Record<string, unknown> | undefined)?.request_id === pending.request_id) : undefined
      const adopted = new Set(tasks.map(item => (item.state.story_result as ComicStoryView | undefined)?.candidate_run_id).filter(Boolean))
      const candidate = tasks.find(item => item.state.story_candidate && ['waiting', 'failed'].includes(item.status)
        && !item.state.story_result && !adopted.has(item.id))
      state.candidateRunId = candidate?.id ?? ''
      if (!pending) return
      if (!run || ['pending', 'running'].includes(run.status)) { state.notice = '请求结果尚未确定，请查询原请求；不会自动重新生成。'; return }
      const unknown = run.state.provider_submission_unknown || run.state.task_status === 'unknown'
      if (unknown) { state.notice = '模型请求结果未确定，需要核对原请求。'; return }
      const view = await api.getComicStory(owner)
      if (ownerEpoch !== epoch || !view) return
      applyView(view, !state.dirty)
      if (run.state.story_candidate && !run.state.story_result && !adopted.has(run.id)) state.candidateRunId = run.id
      state.unknownRequest = null; rememberRequest(null)
      state.notice = run.status === 'failed' ? '原请求已失败，原故事和本地修改已保留。' : '已恢复原请求结果。'
      if (state.dirty) state.conflict = true
    } catch (failure) { if (ownerEpoch === epoch) state.error = failure instanceof Error ? failure.message : '原请求查询失败' }
  }
  /** 已填镜头与勾选图片共同构成本次输入，不能因勾选新图丢掉已有框架。 */
  function generateImages(): ComicImageRef[] {
    const picked = state.images.filter(item => state.selectedImages.includes(storyImageKey(item)))
      .map(item => ({ image_id: item.image_id, version: item.version }))
    const source = [...state.draft.shots.map(shot => shot.image), ...picked]
    const unique = new Map(source.map(item => [storyImageKey(item), item]))
    return [...unique.values()]
  }
  async function mutate(action: 'generate' | 'review' | 'confirm' | 'restore' | 'candidate' | 'export', argument?: number | boolean): Promise<boolean> {
    if (state.operation || state.unknownRequest) return false
    let repaired = 0
    if (action === 'generate') state.draft.shots.forEach((shot, index) => {
      const replacement = imageReplacement(shot)
      if (replacement && state.selectedImages.includes(storyImageKey(replacement))) { repairImage(index); repaired++ }
    })
    const framed = state.draft.shots.length > 0 || !!state.draft.title.trim() || !!state.draft.summary.trim()
    if (action === 'generate' && !state.selectedImages.length && !framed) {
      state.error = '请勾选已采用图片，或先添加镜头再让 AI 整理初稿。'; return false
    }
    // 既没有要求也没有已填框架时，模型没有任何依据，不能拿空提示去生成。
    if (action === 'generate' && !state.draft.instruction.trim() && !framed) {
      state.error = '请填写本轮故事要求，或先填写故事标题、概述与镜头内容。'; return false
    }
    if (action === 'generate' && generateImages().length > 8) {
      state.error = '已填镜头与勾选图片合计最多使用 8 个图片版本，请减少后再整理。'; return false
    }
    const ownerEpoch = epoch, owner = state.owner, requestId = crypto.randomUUID()
    let edits = editEpoch
    let submitted = false
    state.operation = action; state.error = ''; state.notice = ''
    try {
      if (!await save() || ownerEpoch !== epoch || disposed) return false
      if (state.unknownRequest) return false
      if (repaired) state.notice = `已用当前勾选的相同内容图片修复 ${repaired} 个镜头引用，镜头文字已保留。`
      if (action === 'confirm' && !canConfirm.value) {
        state.error = '当前故事尚未通过校验，请先校验或修改后重新校验。'; return false
      }
      edits = editEpoch
      const body = await writeBody(requestId)
      if (ownerEpoch !== epoch || disposed) return false
      const images = generateImages()
      if (action === 'generate' && !images.length) throw new Error('请勾选已采用图片，或先添加镜头再让 AI 整理初稿。')
      rememberRequest({ request_id: requestId, action })
      submitted = true
      let view: ComicStoryView
      if (action === 'generate') {
        view = await api.generateComicStory(owner, { ...body, instruction: state.draft.instruction,
          images, draft: cloneDraft(state.draft) })
      } else if (action === 'review') view = await api.reviewComicStory(owner, body)
      else if (action === 'confirm') view = await api.confirmComicStory(owner, { ...body,
        accept_warnings: argument === true || state.view?.review?.status === 'warning' })
      else if (action === 'restore') view = await api.restoreComicStory(owner, { ...body, version: Number(argument) })
      else if (action === 'candidate') view = await api.adoptComicStoryCandidate(owner, { ...body, candidate_run_id: state.candidateRunId })
      else view = await api.exportComicStory(owner, body)
      if (ownerEpoch !== epoch || disposed) return false
      applyView(view, edits === editEpoch); rememberRequest(null)
      if (edits !== editEpoch) { state.dirty = true; state.notice = '结果已保存，继续保留你刚才的修改。'; void save() }
      if (action === 'candidate') state.candidateRunId = ''
      const versions = await api.getComicStoryVersions(owner)
      if (ownerEpoch === epoch && !disposed) state.versions = versions
      return true
    } catch (failure) {
      if (ownerEpoch !== epoch || disposed) return false
      state.error = failure instanceof Error ? failure.message : '操作没有完成'
      const status = failure instanceof core.CoreApiError ? failure.httpStatus : 0
      if (submitted && (!status || status >= 500)) { state.unknownRequest = { request_id: requestId, action }; await recoverRequest() }
      else {
        rememberRequest(null)
        if (status === 409) { const latest = await api.getComicStory(owner); if (latest && ownerEpoch === epoch) applyView(latest, false); await recoverRequest() }
        else if (submitted && action === 'generate') {
          const message = state.error
          await recoverRequest()
          if (ownerEpoch === epoch && !disposed) state.error = message
        }
      }
      return false
    } finally { if (ownerEpoch === epoch) state.operation = '' }
  }
  async function download(): Promise<void> {
    const ownerEpoch = epoch
    if (!canExport.value || !await mutate('export') || ownerEpoch !== epoch) return
    try {
      const item = state.view?.exports.find(entry => entry.story_version === state.view?.story?.version)
      if (!item) { state.error = '素材包未返回文件，请查询原请求。'; return }
      const url = await api.getArtifactContentUrl(item.artifact_id, true)
      if (!url) throw new Error('素材包文件暂不可读取，请核对导出记录。')
      if (ownerEpoch !== epoch || disposed) { URL.revokeObjectURL(url); return }
      const link = document.createElement('a'); link.href = url; link.download = item.filename; link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (failure) { if (ownerEpoch === epoch) state.error = failure instanceof Error ? failure.message : '素材包下载失败' }
  }
  function reset(): void { epoch++; editEpoch++; if (autosave) clearTimeout(autosave); clearPreviews(); state.owner = ''; state.view = null; state.loaded = false; state.draft = emptyDraft(); state.dirty = false; state.images = []; state.versions = []; state.unknownRequest = null; state.operation = ''; savePromise = null }
  async function dispose(): Promise<void> { await save(); disposed = true; reset() }
  return { state, dirty, confirmed, canConfirm, canExport, saveLabel, load, save, changed, toggleImage, addImage, moveShot,
    removeShot, replaceImage, imageReplacement, repairImage, preview, previewFailed, showImages, recoverRequest, mutate, download, reset, dispose }
}
export type StoryWorkbench = ReturnType<typeof createStoryWorkbench>
