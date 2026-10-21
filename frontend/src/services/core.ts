import type { Conversation, ConversationMessage, CoreApproval, CoreArtifact, CoreBatch, CoreRun, IntentPlan, MediaJob } from '../types'

export interface RuntimeEvent {
  id: number
  run_id: string
  sequence: number
  event_type: string
  node_id: string | null
  payload: Record<string, unknown>
  created_at: string
}

export type CreationMode = 'fast' | 'professional'
export interface ComicProjectContext {
  project: { project_id: string; title: string; current_version: number; director_version?: number | null }
  creative_brief: { original_request: string; version?: number; hard_constraints?: string[]; soft_preferences?: string[]; creative_freedom?: string[] }
}
export interface ComicConversationWork {
  work_id: string; project_id: string; brief_version: number | null; title: string
  original_request: string; current: boolean; director_run_id: string | null
  has_director: boolean; image_count: number
}
export async function getComicConversationWorks(owner: string): Promise<ComicConversationWork[]> {
  return (await coreGet<{ works: ComicConversationWork[] }>(
    `/api/comic/conversations/${encodeURIComponent(owner)}/works`, `comic-works:${owner}`))?.works ?? []
}
export interface ComicImageTask {
  work_id: string; task_id: string; project_id: string; title: string; conversation_id: string
  project_version: number; director_version: number | null
  image_count?: number; adopted_image_count?: number
  settings_refs: { source_asset_id: string; source_version: number; local_asset_id: string }[]
  settings_updates?: { asset_id: string; version: number }[]
  history_source_only?: boolean
  legacy_source?: { project_id: string; brief_version: number | null; director_run_id: string | null; conversation_id: string; source_id: string }
}
export interface ComicWork { work_id: string; title: string; version: number; conversation_id: string; updated_at?: string; story_conversation_id: string; tasks: ComicImageTask[]; plan_recoveries?: { source_run_id: string; task_id: string; run_id: string }[]; migration?: { request: Record<string, unknown>; completed: boolean } | null }
export function getComicWorkConversation(workId: string): Promise<{ conversation_id: string; messages: ConversationMessage[]; related_run_ids: string[] } | null> {
  return coreGet(`/api/comic/works/${encodeURIComponent(workId)}/conversation`, `work-conversation:${workId}`)
}
export function sendComicWorkTurn(workId: string, body: Record<string, unknown>): Promise<(DirectorExecution | ConversationTurnResponse) & { task_id?: string; image_action?: string }> {
  return corePost(`/api/comic/works/${encodeURIComponent(workId)}/turns`, body)
}
export interface ComicTurnStatus {
  phase: string | null; result: Record<string, unknown> | null
  error?: { safe_message: string; trace_id?: string; error_id?: string }
}
export function getComicWorkTurn(workId: string, requestId: string): Promise<ComicTurnStatus | null> {
  return coreGet(`/api/comic/works/${encodeURIComponent(workId)}/requests/${encodeURIComponent(requestId)}`, `work-request:${workId}:${requestId}`)
}
export function deleteComicWork(work: ComicWork): Promise<{ deleted: boolean }> {
  return corePost(`/api/comic/works/${encodeURIComponent(work.work_id)}/delete`, { request_id: crypto.randomUUID(), expected_version: work.version })
}
export interface ComicLegacyPreview {
  conversation_id: string; preview_sha256: string; unassigned_message_ids: string[]
  groups: { work_id: string; project_id: string; brief_version: number | null; title: string; original_request: string; images: (ComicImageView & { selected: boolean })[]; has_director: boolean; messages: unknown[] }[]
  story: ComicStory | null
}
export function previewComicLegacy(owner: string): Promise<ComicLegacyPreview | null> { return coreGet(`/api/comic/works/legacy-preview?conversation_id=${encodeURIComponent(owner)}`, `legacy-preview:${owner}`) }
export function importComicLegacy(body: Record<string, unknown>): Promise<ComicWork> { return corePost('/api/comic/works/legacy-import', body) }
export async function listComicWorks(): Promise<ComicWork[]> { return (await coreGet<{ works: ComicWork[] }>('/api/comic/works', 'comic-workspaces'))?.works ?? [] }
export function getComicWork(id: string): Promise<ComicWork | null> { return coreGet(`/api/comic/works/${encodeURIComponent(id)}`, `comic-workspace:${id}`) }
export function createComicWork(title = '未命名作品'): Promise<ComicWork> { return corePost('/api/comic/works', { request_id: crypto.randomUUID(), title }) }
export function renameComicWork(work: ComicWork, title: string): Promise<ComicWork> { return corePost(`/api/comic/works/${encodeURIComponent(work.work_id)}/rename`, { request_id: crypto.randomUUID(), expected_version: work.version, title }) }
export function createComicImageTask(work: ComicWork, requirement = ''): Promise<ComicImageTask> {
  return corePost(`/api/comic/works/${encodeURIComponent(work.work_id)}/image-tasks`, { request_id: crypto.randomUUID(), expected_version: work.version,
    title: requirement.slice(0, 28) || '新画面', requirement })
}
export function sendComicTaskTurn(workId: string, taskId: string, body: { request_id: string; conversation_id: string; expected_project_version: number; expected_director_version: number | null; text: string; creation_mode: CreationMode; image?: ComicImageRef | null }): Promise<DirectorExecution | (ConversationTurnResponse & { image_action?: string })> {
  return corePost(`/api/comic/works/${encodeURIComponent(workId)}/image-tasks/${encodeURIComponent(taskId)}/turns`, body)
}
export function getComicTaskTurn(workId: string, taskId: string, requestId: string): Promise<ComicTurnStatus | null> {
  return coreGet(`/api/comic/works/${encodeURIComponent(workId)}/image-tasks/${encodeURIComponent(taskId)}/requests/${encodeURIComponent(requestId)}`, `task-request:${taskId}:${requestId}`)
}
export async function getComicWorkSettings(workId: string): Promise<ComicAssetView[]> {
  return (await coreGet<{ assets: ComicAssetView[] }>(`/api/comic/works/${encodeURIComponent(workId)}/settings`, `work-settings:${workId}`))?.assets ?? []
}
export function publishComicWorkSetting(workId: string, body: Record<string, unknown>): Promise<ComicAssetView> {
  return corePost(`/api/comic/works/${encodeURIComponent(workId)}/settings`, body)
}
export function syncComicTaskSettings(workId: string, taskId: string, body: Record<string, unknown>): Promise<ComicImageTask> {
  return corePost(`/api/comic/works/${encodeURIComponent(workId)}/image-tasks/${encodeURIComponent(taskId)}/settings-sync`, body)
}
export interface DirectorNodeSummary {
  stage: string
  status: string
  input_versions: Record<string, number>
  output_summary: string | null
  output: Record<string, Record<string, unknown>>
}
export type GenerationMode = 'FIRST_GENERATION' | 'USER_EDIT' | 'NEW_CONTEXT'
export interface CreativeContextLock {
  character: string; world: string; visual_style: string; camera_style: string
  environment: string; fixed_attributes: string[]
  director_version: number; brief_version: number
  bible_sources: Record<string, unknown>
}
export interface ContextDiff {
  instruction: string | null
  changes: { field_path: string; before: unknown; after: unknown }[]
  affected_fields: string[]; impact_scope: string[]
  potential_conflicts: CriticFeedback[]; recommended_sync_changes: string[]
}
export interface DirectorExecution {
  run_id: string
  status: string
  recovery_required: boolean
  director_spec: Record<string, unknown> | null
  generation_mode?: GenerationMode
  review_required?: boolean
  /** 产出当前方案的原始 Run；用于把错配的审核结论送回其真实来源。 */
  source_run_id?: string
  creative_context_lock?: CreativeContextLock
  context_diff?: ContextDiff | null
  shot_continuation?: { storyboard: ComicStoryboardView; shot: ComicShotView; prompt: Record<string, unknown> }
  director_execution_summary: {
    mode: CreationMode
    generation_mode?: GenerationMode
    review_required?: boolean
    context_diff?: ContextDiff | null
    current_stage: string
    status_label: string
    available_actions: string[]
    stages: DirectorNodeSummary[]
    error_id: string | null
    critic_result?: { verdict: string; public_summary: string; findings: { code: string; evidence: string; suggested_action: string }[] } | null
    critic_feedback?: CriticFeedback[]
  }
}
export interface ConversationTurnResponse {
  conversation_response: { action: 'chat' | 'clarify'; content: string; message: ConversationMessage }
  context?: Record<string, unknown>
}

export interface CriticFeedback {
  level: 'blocking' | 'warning'; issue_type: string; message: string; reason: string
  suggestion: string; affected_field: string; affected_field_label: string; impact: string
}

export async function createComicProject(request: string): Promise<ComicProjectContext> {
  return corePost('/api/comic/projects', {
    title: request.slice(0, 100), brief: { original_request: request },
  })
}
export async function getComicProject(projectId: string): Promise<ComicProjectContext | null> {
  return coreGet(`/api/comic/projects/${encodeURIComponent(projectId)}`, `comic-project:${projectId}`)
}
export async function getDirectorExecutions(projectId: string): Promise<DirectorExecution[] | null> {
  const result = await coreGet<{ tasks: unknown[] }>(
    `/api/comic/projects/${encodeURIComponent(projectId)}/tasks`, `comic-director-tasks:${projectId}`,
  )
  // 请求被并发取消时 coreGet 返回 null；此时必须让调用方区分“取消”与“空列表”，
  // 否则轮询会把既有产出整体清空。
  if (!result) return null
  return (result.tasks ?? []).filter((item): item is DirectorExecution =>
    !!item && typeof item === 'object' && 'director_execution_summary' in item,
  )
}
export async function getComicImageRequests(projectId: string, owner: string): Promise<CoreRun[] | null> {
  const response = await coreGet<{ tasks: CoreRun[] }>(`/api/comic/projects/${encodeURIComponent(projectId)}/tasks`, `image-requests:${projectId}:${owner}`)
  return response ? response.tasks.filter(item => item.state?.conversation_id === owner && item.state?.task_type === 'image.edit') : null
}
export async function createDirectorExecution(
  projectId: string, body: Record<string, unknown>,
): Promise<DirectorExecution | ConversationTurnResponse> {
  return corePost(`/api/comic/projects/${encodeURIComponent(projectId)}/director-spec`, body)
}

// Workspace projections of existing Comic APIs; no parallel domain store.
export interface ComicAssetView {
  asset_id: string; name: string; version: number; pinned_version: number | null; state: string
  aliases?: string[]; tags?: string[]
  details: Record<string, unknown>; fixed_constraints: string[]; reference_artifact_ids: string[]
  /** 删除等写操作返回的新作品版本；用于批量删除时链式续接乐观并发。 */
  project_version?: number
}
export interface ComicStoryboardView {
  storyboard_id: string; title: string; description: string; version: number; director_spec_version: number; status: string
}
export interface ComicShotView {
  shot_id: string; storyboard_id: string; sequence_number: number; purpose: string; subject: string; action: string
  environment: string; version: number; status: string; character_asset_versions: { asset_id: string; version: number }[]
  scene_asset_versions: { asset_id: string; version: number }[]; style_version: { asset_id: string; version: number } | null
}
export async function getComicAssets(id: string): Promise<ComicAssetView[]> {
  return (await coreGet<{ assets: ComicAssetView[] }>(`/api/comic/projects/${encodeURIComponent(id)}/assets`, `comic-assets:${id}`))?.assets ?? []
}
/* 项目资产删除：复用既有 change_comic_asset 的 delete 动作，资产版本 + 作品版本双校验。
   删除是状态变更（state='deleted'），不是物理删除；历史版本仍然保留在作品版本链上。 */
export function deleteComicAsset(projectId: string, assetId: string,
  assetVersion: number, projectVersion: number): Promise<ComicAssetView> {
  return corePost(`/api/comic/projects/${encodeURIComponent(projectId)}/assets/${encodeURIComponent(assetId)}/delete`, {
    expected_project_version: projectVersion, expected_asset_version: assetVersion,
  })
}
export async function getComicStoryboards(id: string): Promise<ComicStoryboardView[] | null> {
  return (await coreGet<{ storyboards: ComicStoryboardView[] }>(`/api/comic/projects/${encodeURIComponent(id)}/storyboards`, `comic-boards:${id}`))?.storyboards ?? null
}
export function createComicStoryboard(id: string, projectVersion: number, task: string, assetIds: string[] = []): Promise<{ storyboard: ComicStoryboardView; shots: ComicShotView[] }> {
  return corePost(`/api/comic/projects/${encodeURIComponent(id)}/storyboards`, {
    expected_project_version: projectVersion, generate: true, task, asset_ids: assetIds,
  })
}
export async function getComicShots(id: string): Promise<ComicShotView[] | null> {
  return (await coreGet<{ shots: ComicShotView[] }>(`/api/comic/storyboards/${encodeURIComponent(id)}/shots`, `comic-shots:${id}`))?.shots ?? null
}
export async function getDirectorVersions(id: string): Promise<Record<string, unknown>[]> {
  return (await coreGet<{ versions: Record<string, unknown>[] }>(`/api/comic/projects/${encodeURIComponent(id)}/director-spec/versions`, `comic-director-versions:${id}`))?.versions ?? []
}
export function getCurrentDirector(id: string): Promise<Record<string, unknown> | null> {
  return coreGet(`/api/comic/projects/${encodeURIComponent(id)}/director-spec`, `comic-director-current:${id}`)
}
export function restoreDirectorVersion(id: string, version: number, projectVersion: number): Promise<Record<string, unknown>> {
  return corePost(`/api/comic/projects/${encodeURIComponent(id)}/director-spec/restore`, { version, expected_project_version: projectVersion })
}
/* 删除单个历史导演方案版本：后端只删该版本行并追加一次作品修订。
   当前正在使用的版本会被后端明确拒绝——这里不做前端预判，避免两边规则漂移。 */
export function deleteDirectorVersion(id: string, version: number, projectVersion: number): Promise<{ deleted_version: number; project_version: number }> {
  return corePost(`/api/comic/projects/${encodeURIComponent(id)}/director-spec/delete`, { version, expected_project_version: projectVersion })
}
export function saveDirectorDraft(id: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
  return corePost(`/api/comic/projects/${encodeURIComponent(id)}/director-spec`, body)
}
export function confirmDirectorVersion(id: string, version: number, projectVersion: number): Promise<Record<string, unknown>> {
  return corePost(`/api/comic/projects/${encodeURIComponent(id)}/director-spec/confirm`, { version, expected_project_version: projectVersion })
}
export async function getComicPromptVersions(id: string): Promise<Record<string, unknown>[] | null> {
  return (await coreGet<{ versions: Record<string, unknown>[] }>(`/api/comic/shots/${encodeURIComponent(id)}/prompt/versions`, `comic-prompt-versions:${id}`))?.versions ?? null
}
export function compileComicPrompt(id: string, projectVersion: number, shotVersion: number,
  externalConversationId?: string, completePrompt = false): Promise<Record<string, unknown>> {
  return corePost(`/api/comic/shots/${encodeURIComponent(id)}/prompt/compile`, {
    expected_project_version: projectVersion, expected_shot_version: shotVersion,
    ...(externalConversationId ? { image_mode: 'external', conversation_id: externalConversationId } : {}),
    ...(completePrompt ? { complete_prompt: true } : {}),
  })
}
export async function getProjectPrompts(projectId: string): Promise<Record<string, unknown>[] | null> {
  return (await coreGet<{ prompts: Record<string, unknown>[] }>(
    `/api/comic/projects/${encodeURIComponent(projectId)}/prompts`, `comic-prompts:${projectId}`,
  ))?.prompts ?? null
}
export function confirmComicPrompt(id: string, projectVersion: number, version: number,
  conversationId: string): Promise<Record<string, unknown>> {
  return corePost(`/api/comic/shots/${encodeURIComponent(id)}/prompt/confirm`, {
    expected_project_version: projectVersion, expected_version: version, conversation_id: conversationId,
  })
}
export async function uploadComicExternalImage(id: string, file: File,
  source: Record<string, unknown>): Promise<Record<string, unknown>> {
  return corePost(`/api/comic/shots/${encodeURIComponent(id)}/external-image`, {
    ...source, data_url: await imageDataUrl(file), filename: file.name,
  })
}
function imageDataUrl(file: File): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(new Error('无法读取所选图片'))
    reader.readAsDataURL(file)
  })
}

export interface ComicImageRef { image_id: string; version: number }
export interface ComicImageView extends ComicImageRef {
  artifact_id: string; filename: string; project_id: string; conversation_id: string
  /* 内容指纹：版本号会被复用，报告归属以此为准（旧分镜投影也可能没有）。 */
  sha256?: string
  mime_type?: string; available?: boolean; unavailable_reason?: string | null; lifecycle_state?: string | null
  source_project_title?: string
  parent?: ComicImageRef | null; shot_id?: string; label?: string
  instruction_artifact_id?: string | null
  selection?: { selected_version: number; selection_version: number; available?: boolean; unavailable_reason?: string | null } | null
}
export interface ComicImageFinding {
  finding_id: string; observation: string; suggested_change: string; confidence: number
  uncertainty?: string; region?: number[] | null
}
interface ComicImageBasis {
  project_id: string; conversation_id: string; source_image: ComicImageRef
  task_context: { task: string }
  constraints?: { requirement: string; target?: string }[]
  /* 报告实际使用的图片（第 0 项为目标图）。版本号会被复用，sha256 才是内容身份。 */
  images?: { image_id: string; version: number; artifact_id: string; sha256: string }[]
}
export interface ComicImageAnalysis {
  artifact_id: string; basis: ComicImageBasis
  output: { summary: string; findings: ComicImageFinding[]; protected_features: string[]; limitations: string[] }
}
export interface ComicEditInstruction {
  artifact_id: string; run_id?: string; basis: ComicImageBasis; analysis_artifact_id: string | null; round_id?: string | null
  scope: { user_instruction: string; selected_findings: ComicImageFinding[]; protected_features: string[]; edit_scope?: ComicEditScope | null }
  output: { positive_prompt: string; negative_prompt: string; summary: string }
  requires_confirmation: boolean
  confirmation?: { confirmed: boolean } | null
}
export interface ComicEditDiagnostics {
  stage: string; diagnostic_artifact_id: string; run_id: string; source_image: ComicImageRef; round_id: string | null
  issues: { code: string; reason: string; requirement_ids: string[]; finding_ids: string[]; region_ids: string[]; constraint_ids: string[] }[]
}
export interface ComicMediaWrite { request_id: string; conversation_id: string; expected_project_version: number }
export interface ComicStoryShot {
  shot_id: string; image: ComicImageRef; action: string; camera: string
  duration_seconds: number | null; transition: string
}
export interface ComicStoryDraft { title: string; summary: string; instruction: string; shots: ComicStoryShot[] }
export interface ComicStoryImage extends ComicImageRef {
  artifact_id: string; source_project_id: string; sha256: string; mime_type: string; filename: string
}
export interface ComicStory extends ComicStoryDraft {
  story_id: string; version: number; anchor_project_id: string; content_sha256: string
  fixed_images: (ComicImageView & { sha256: string; mime_type: string })[]; created_at?: string; source?: string; run_id?: string
}
export interface ComicStoryReview {
  validation_requested?: boolean
  status: 'pass' | 'warning' | 'needs_revision'; story_version?: number; content_sha256?: string
  /* code = 错误类型，suggestion = 修改建议；两者都用于把问题指到具体镜头与字段。 */
  findings: { severity: 'warning' | 'error'; shot_id: string | null; field: string; message: string
    evidence: string; code?: string; suggestion?: string }[]
}
export interface ComicStoryExport { artifact_id: string; filename: string; content_url: string; sha256: string; story_version: number; available?: boolean }
export interface ComicStoryView {
  story: ComicStory | null; review: ComicStoryReview | null
  confirmation: { confirmed: boolean; story_version?: number; content_sha256?: string } | null
  export_text: string; project_version: number | null
  anchor_project_id?: string | null
  manifest: { story_id: string; version: number; content_sha256: string; images: ComicStoryImage[]
    shots: { shot_id: string; sequence_number: number; image_id: string; image_version: number; filename: string; action: string; camera: string; duration_seconds: number | null; transition: string }[] } | null
  missing_images: { shot_id: string; image_id?: string; image_version?: number; reason: string; source_project_id?: string }[]
  exports: ComicStoryExport[]; candidate_run_id?: string | null; run_id?: string
}
export interface ComicStoryWrite extends ComicMediaWrite { anchor_project_id: string; expected_story_version: number }
function comicStoryPath(owner: string): string { return owner.startsWith('work:')
  ? `/api/comic/works/${encodeURIComponent(owner.slice(5))}/story` : `/api/comic/conversations/${encodeURIComponent(owner)}/story` }
function storyWrite(owner: string, body: ComicStoryWrite): Record<string, unknown> {
  const { conversation_id, ...rest } = body
  return owner.startsWith('work:') ? rest : { ...rest, conversation_id }
}
export function getComicStory(owner: string): Promise<ComicStoryView | null> {
  return coreGet(comicStoryPath(owner), `comic-story:${owner}`)
}
export interface ComicStoryImagePage {
  images: ComicImageView[]; unavailable_images?: (ComicImageRef & { reason: string })[]
}
export async function getComicStoryImages(owner: string): Promise<ComicStoryImagePage | null> {
  return coreGet(`${comicStoryPath(owner)}/images`, `comic-story-images:${owner}`)
}
export async function getComicStoryVersions(owner: string): Promise<ComicStory[]> {
  return (await coreGet<{ versions: ComicStory[] }>(`${comicStoryPath(owner)}/versions`, `comic-story-versions:${owner}`))?.versions ?? []
}
export async function getComicStoryRequests(owner: string): Promise<CoreRun[]> {
  return (await coreGet<{ tasks: CoreRun[] }>(`${comicStoryPath(owner)}/requests`, `comic-story-requests:${owner}`))?.tasks ?? []
}
export function saveComicStory(owner: string, body: ComicStoryWrite & { draft: ComicStoryDraft }): Promise<ComicStoryView> {
  return owner.startsWith('work:') ? corePost(`${comicStoryPath(owner)}/save`, storyWrite(owner, body), 'POST', true)
    : corePost(comicStoryPath(owner), { ...body }, 'PUT', true)
}
export function generateComicStory(owner: string, body: ComicStoryWrite & { instruction: string; images: ComicImageRef[]; draft?: ComicStoryDraft }): Promise<ComicStoryView> {
  return corePost(`${comicStoryPath(owner)}/generate`, storyWrite(owner, body))
}
export function reviewComicStory(owner: string, body: ComicStoryWrite): Promise<ComicStoryView> {
  return corePost(`${comicStoryPath(owner)}/review`, storyWrite(owner, body))
}
export function confirmComicStory(owner: string, body: ComicStoryWrite & { accept_warnings?: boolean }): Promise<ComicStoryView> {
  return corePost(`${comicStoryPath(owner)}/confirm`, storyWrite(owner, body))
}
export function restoreComicStory(owner: string, body: ComicStoryWrite & { version: number }): Promise<ComicStoryView> {
  return corePost(`${comicStoryPath(owner)}/restore`, storyWrite(owner, body))
}
export function adoptComicStoryCandidate(owner: string, body: ComicStoryWrite & { candidate_run_id: string }): Promise<ComicStoryView> {
  return corePost(`${comicStoryPath(owner)}/adopt-candidate`, storyWrite(owner, body))
}
export function exportComicStory(owner: string, body: ComicStoryWrite): Promise<ComicStoryView> {
  return corePost(`${comicStoryPath(owner)}/exports`, storyWrite(owner, body))
}
export interface ComicEditRegion { region_id: string; label?: string; bounds: number[]; finding_id?: string | null; instruction: string }
export interface ComicInstructionTarget { mode: 'whole' | 'selected_regions' | 'region'; region_id?: string | null }
export interface ComicEditScope {
  mode: 'whole' | 'regions' | 'mixed'; regions: ComicEditRegion[]
  whole_finding_ids?: string[]; instruction_target?: ComicInstructionTarget | null
}
export interface ComicReviewDraft {
  image: ComicImageRef; round_id: string; task: string; analysis_artifact_id: string | null
  selected_finding_ids: string[]; edit_scope: ComicEditScope | null; instruction: string
  dismissed_finding_region_ids?: string[]
  instruction_artifact_id: string | null; phase: 'editing' | 'review'
}
export interface ComicWorkbenchState { image: ComicImageRef | null; edit_image?: ComicImageRef | null; auto_analyze: boolean }
export interface ComicSavedState<T> { version: number; state: T | null; unavailable_images?: (ComicImageRef & { reason: string })[] }
export interface ComicReportPage<T> { items: T[]; next_cursor: string | null; image_available?: boolean; unavailable_reason?: string | null; unavailable_items?: { artifact_id: string; reason: string }[] }
export interface ComicComparison {
  artifact_id: string; before: ComicImageView; after: ComicImageView
  output: { summary: string; changes: { check_id: string; status: string; evidence: string }[]; preservation: { check_id: string; status: string; evidence: string }[]; limitations: string[] }
}
export function getComicWorkbench(projectId: string, owner: string): Promise<ComicSavedState<ComicWorkbenchState> | null> {
  return coreGet(`/api/comic/projects/${encodeURIComponent(projectId)}/image-workbench?conversation_id=${encodeURIComponent(owner)}`, `workbench:${projectId}:${owner}`)
}
export function saveComicWorkbench(projectId: string, owner: string, version: number, state: ComicWorkbenchState): Promise<ComicSavedState<ComicWorkbenchState>> {
  return corePost(`/api/comic/projects/${encodeURIComponent(projectId)}/image-workbench`, { ...state, conversation_id: owner, expected_version: version, request_id: crypto.randomUUID() }, 'PUT', true)
}
export function getComicReviewDraft(image: ComicImageRef, owner: string): Promise<ComicSavedState<ComicReviewDraft> | null> {
  return coreGet(`/api/comic/images/${encodeURIComponent(image.image_id)}/review-draft?version=${image.version}&conversation_id=${encodeURIComponent(owner)}`, `review-draft:${owner}:${image.image_id}:${image.version}`)
}
export function saveComicReviewDraft(owner: string, version: number, draft: ComicReviewDraft, requestId: string): Promise<ComicSavedState<ComicReviewDraft>> {
  return corePost(`/api/comic/images/${encodeURIComponent(draft.image.image_id)}/review-draft`, { ...draft, conversation_id: owner, expected_version: version, request_id: requestId }, 'PUT', true)
}
export function getComicImageVersions(imageId: string, owner: string, before?: number): Promise<{ versions: ComicImageView[]; next_before: number | null; selection: ComicImageView['selection'] } | null> {
  return coreGet(`/api/comic/images/${encodeURIComponent(imageId)}/versions?conversation_id=${encodeURIComponent(owner)}&limit=20${before ? `&before=${before}` : ''}`, `image-versions:${owner}:${imageId}:${before ?? ''}`)
}
export function getComicImageReports<T>(projectId: string, owner: string, kind: 'image-analyses' | 'edit-instructions' | 'image-comparisons', image: ComicImageRef, before?: string): Promise<ComicReportPage<T> | null> {
  const query = new URLSearchParams({ conversation_id: owner, image_id: image.image_id, image_version: String(image.version), limit: '20' })
  if (before) query.set('before', before)
  return coreGet(`/api/comic/projects/${encodeURIComponent(projectId)}/${kind}?${query}`, `image-reports:${owner}:${image.image_id}:${image.version}:${kind}:${before ?? ''}`)
}
export function getComicImageReport<T>(projectId: string, owner: string, kind: 'image-analyses' | 'edit-instructions', id: string): Promise<T | null> {
  return coreGet(`/api/comic/projects/${encodeURIComponent(projectId)}/${kind}/${encodeURIComponent(id)}?conversation_id=${encodeURIComponent(owner)}`, `image-report:${owner}:${id}`)
}
export function compareComicImages(projectId: string, body: ComicMediaWrite & { before: ComicImageRef; after: ComicImageRef; instruction_artifact_id: string }): Promise<ComicComparison> {
  return corePost(`/api/comic/projects/${encodeURIComponent(projectId)}/image-comparisons`, { ...body })
}
export function selectComicImage(imageId: string, body: ComicMediaWrite & { version: number; expected_selection_version: number }): Promise<unknown> {
  return corePost(`/api/comic/images/${encodeURIComponent(imageId)}/selection`, { ...body }, 'PUT')
}
/* 删除一张已导入图片及其全部版本：后端会拒绝故事已固定或正作参考资产的图片。
   删除成功后作品版本 +1，调用方需重新读取图片列表与工作台。 */
export function deleteComicImage(projectId: string, imageId: string, body: ComicMediaWrite): Promise<{ image_id: string; deleted_versions: number[]; project_version: number }> {
  return corePost(`/api/comic/projects/${encodeURIComponent(projectId)}/images/${encodeURIComponent(imageId)}/delete`, { ...body })
}
export async function restoreComicImageFile(image: ComicImageRef, file: File, body: ComicMediaWrite): Promise<unknown> {
  return corePost(`/api/comic/images/${encodeURIComponent(image.image_id)}/restore-file`, { ...body, image, filename: file.name, data_url: await imageDataUrl(file) })
}
export async function getComicImages(projectId: string, conversationId: string, briefVersion?: number): Promise<ComicImageView[]> {
  return (await coreGet<{ images: ComicImageView[] }>(
    `/api/comic/projects/${encodeURIComponent(projectId)}/images?conversation_id=${encodeURIComponent(conversationId)}${briefVersion === undefined ? '' : `&brief_version=${briefVersion}`}`,
    `comic-images:${projectId}:${conversationId}:${briefVersion ?? ''}`))?.images ?? []
}
export async function importComicImage(projectId: string, file: File, body: ComicMediaWrite & { image_id?: string; expected_image_version?: number; parent?: ComicImageRef; instruction_artifact_id?: string }): Promise<ComicImageView> {
  return corePost(`/api/comic/projects/${encodeURIComponent(projectId)}/images`, {
    ...body, filename: file.name, data_url: await imageDataUrl(file),
  })
}
export async function getComicImageAnalyses(projectId: string, conversationId: string): Promise<ComicImageAnalysis[]> {
  return (await coreGet<{ items: ComicImageAnalysis[] }>(
    `/api/comic/projects/${encodeURIComponent(projectId)}/image-analyses?conversation_id=${encodeURIComponent(conversationId)}`,
    `comic-analyses:${projectId}:${conversationId}`))?.items ?? []
}
export async function getComicEditInstructions(projectId: string, conversationId: string): Promise<ComicEditInstruction[]> {
  return (await coreGet<{ items: ComicEditInstruction[] }>(
    `/api/comic/projects/${encodeURIComponent(projectId)}/edit-instructions?conversation_id=${encodeURIComponent(conversationId)}`,
    `comic-edit-instructions:${projectId}:${conversationId}`))?.items ?? []
}
export function analyzeComicImage(projectId: string, body: ComicMediaWrite & { image: ComicImageRef; task: string }): Promise<ComicImageAnalysis> {
  return corePost(`/api/comic/projects/${encodeURIComponent(projectId)}/image-analyses`, { ...body })
}
export function createComicEditInstruction(projectId: string, body: ComicMediaWrite & {
  image: ComicImageRef; task: string; analysis_artifact_id?: string | null; selected_finding_ids: string[]; instruction: string; edit_scope?: ComicEditScope | null; round_id?: string
}): Promise<ComicEditInstruction> {
  return corePost(`/api/comic/projects/${encodeURIComponent(projectId)}/edit-instructions`, { ...body })
}
export function confirmComicEditInstruction(projectId: string, artifactId: string, body: ComicMediaWrite): Promise<unknown> {
  return corePost(`/api/comic/projects/${encodeURIComponent(projectId)}/edit-instructions/${encodeURIComponent(artifactId)}/confirm`, {
    ...body, instruction_artifact_id: artifactId,
  })
}
export function saveComicPrompt(id: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
  return corePost(`/api/comic/shots/${encodeURIComponent(id)}/prompt`, body, 'PUT')
}
/* 把某个历史 Prompt 版本恢复为新版本：后端会写一条 source='restored' 的新版本，
   原版本全部保留。expected_version 用于乐观锁，来源版本漂移时后端会明确拒绝。 */
export function restoreComicPrompt(id: string, version: number, expectedVersion: number,
  projectVersion: number): Promise<Record<string, unknown>> {
  return corePost(`/api/comic/shots/${encodeURIComponent(id)}/prompt/restore`, {
    version, expected_version: expectedVersion, expected_project_version: projectVersion,
  })
}
/* 删除单个历史 Prompt 版本：与 restore 同源，只影响 comic_entity_versions 里的一行。
   当前版本由后端拒绝；删除成功后作品版本 +1，调用方需用返回的 project_version 续接。 */
export function deleteComicPromptVersion(shotId: string, version: number,
  projectVersion: number): Promise<{ shot_id: string; deleted_version: number; project_version: number }> {
  return corePost(`/api/comic/shots/${encodeURIComponent(shotId)}/prompt/delete`, {
    version, expected_project_version: projectVersion,
  })
}

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '')
  ?? (import.meta.env.DEV ? 'http://127.0.0.1:8000' : '')
let token = document.querySelector<HTMLMetaElement>('meta[name="studio-token"]')?.content ?? ''
let tokenPromise: Promise<string> | null = null

function api(path: string): string { return `${API_BASE}${path}` }

export class CoreApiError extends Error {
  constructor(
    message: string,
    public readonly httpStatus: number,
    public readonly traceId: string | null,
    public readonly errorId: string | null,
    public readonly reviewDiagnostics: ComicEditDiagnostics | null = null,
  ) {
    super(errorId ? `${message} · 错误编号：${errorId}` : message)
    this.name = 'CoreApiError'
  }
}

async function apiFailure(response: Response, fallback: string): Promise<CoreApiError> {
  const body = await response.json().catch(() => ({})) as {
    error?: string; safe_message?: string; trace_id?: string; error_id?: string; provider_error_code?: string; review_diagnostics?: ComicEditDiagnostics
  }
  const message = body.safe_message ?? body.error ?? fallback
  return new CoreApiError(
    body.provider_error_code === 'Arrearage'
      ? `${message}：模型服务账户欠费或余额不足，请检查阿里云账户状态（Arrearage）。`
      : body.provider_error_code ? `${message}（供应商错误：${body.provider_error_code}）` : message,
    response.status,
    body.trace_id ?? response.headers.get('X-Trace-ID'),
    body.error_id ?? null,
    body.review_diagnostics ?? null,
  )
}

async function ensureToken(): Promise<string> {
  if (token && token !== '__TOKEN__') return token
  tokenPromise ??= fetch(api('/api/session')).then(async (response) => {
    if (!response.ok) throw await apiFailure(response, '无法连接 Kantoku 后端')
    const result = await response.json() as { token: string }
    token = result.token
    return token
  }).finally(() => { tokenPromise = null })
  return tokenPromise
}

/* 每个数据源只保留最后一次生效响应：新请求取消旧请求，旧响应即使晚到也不会覆盖新状态。 */
const inflight = new Map<string, AbortController>()
const appliedSeq = new Map<string, number>()
let globalSeq = 0

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

async function coreGet<T>(path: string, key: string): Promise<T | null> {
  await ensureToken()
  inflight.get(key)?.abort()
  const controller = new AbortController()
  inflight.set(key, controller)
  const seq = ++globalSeq
  try {
    const sentToken = token
    let response = await fetch(api(path), {
      headers: { 'X-Studio-Token': sentToken },
      signal: controller.signal,
    })
    if (response.status === 403) {
      if (token === sentToken) token = ''
      const refreshedToken = await ensureToken()
      response = await fetch(api(path), { headers: { 'X-Studio-Token': refreshedToken }, signal: controller.signal })
    }
    if (!response.ok) throw await apiFailure(response, '请求没有完成')
    const result = (await response.json()) as T
    if (seq < (appliedSeq.get(key) ?? 0)) return null
    appliedSeq.set(key, seq)
    return result
  } catch (error) {
    if (isAbort(error)) return null
    throw error
  } finally {
    if (inflight.get(key) === controller) inflight.delete(key)
  }
}

async function corePost<T>(path: string, body: Record<string, unknown> = {}, method: 'POST' | 'PUT' = 'POST', keepalive = false): Promise<T> {
  await ensureToken()
  const response = await fetch(api(path), {
    method,
    keepalive,
    headers: { 'Content-Type': 'application/json', 'X-Studio-Token': token },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw await apiFailure(response, '请求没有完成')
  const result = (await response.json()) as T
  return result
}

export function disposeAll(): void {
  inflight.forEach((controller) => controller.abort())
  inflight.clear()
}

export async function getRuns(): Promise<CoreRun[] | null> {
  const result = await coreGet<{ runs: CoreRun[] }>('/api/runs', 'runs')
  return result?.runs ?? null
}

export async function getRun(runId: string): Promise<CoreRun | null> {
  return coreGet<CoreRun>(`/api/runs/${encodeURIComponent(runId)}`, `run:${runId}`)
}

export async function getApprovals(scope = 'approvals'): Promise<CoreApproval[] | null> {
  const result = await coreGet<{ approvals: CoreApproval[] }>('/api/approvals', scope)
  return result?.approvals ?? null
}

export async function getArtifacts(runId?: string, lifecycle?: 'active' | 'trashed'): Promise<CoreArtifact[] | null> {
  const query = new URLSearchParams()
  if (runId) query.set('run_id', runId)
  if (lifecycle) query.set('lifecycle', lifecycle)
  const path = `/api/artifacts${query.size ? '?' + query : ''}`
  const result = await coreGet<{ artifacts: CoreArtifact[] }>(path, `artifacts:${runId ?? 'all'}:${lifecycle ?? ''}`)
  return result?.artifacts ?? null
}

export type ArtifactLifecycleAction = 'trash' | 'restore' | 'purge'

export function changeArtifactLifecycle(artifact: CoreArtifact, action: ArtifactLifecycleAction): Promise<CoreArtifact> {
  return corePost<CoreArtifact>(`/api/artifacts/${encodeURIComponent(artifact.id)}/${action}`, {
    expected_lifecycle_version: artifact.lifecycle_version ?? 1,
  })
}

export async function getArtifactContentBlob(artifactId: string, reportFailure = false): Promise<Blob | null> {
  await ensureToken()
  const response = await fetch(api(`/api/artifacts/${encodeURIComponent(artifactId)}/content`), {
    headers: { 'X-Studio-Token': token },
  })
  if (!response.ok) {
    if (reportFailure) throw await apiFailure(response, '产物文件读取失败')
    return null
  }
  return response.blob()
}

export async function getArtifactContentUrl(artifactId: string, reportFailure = false): Promise<string | null> {
  const content = await getArtifactContentBlob(artifactId, reportFailure)
  return content ? URL.createObjectURL(content) : null
}

export async function getArtifact(artifactId: string): Promise<CoreArtifact | null> {
  return coreGet<CoreArtifact>(`/api/artifacts/${encodeURIComponent(artifactId)}`, `artifact:${artifactId}`)
}

export async function getTaskImageUrl(requestId: string, reportFailure = false): Promise<string | null> {
  await ensureToken()
  const response = await fetch(api(`/media/${encodeURIComponent(requestId)}`), {
    headers: { 'X-Studio-Token': token },
  })
  if (!response.ok) {
    if (reportFailure) throw await apiFailure(response, '历史图片读取失败')
    return null
  }
  return URL.createObjectURL(await response.blob())
}

export async function getBatches(): Promise<CoreBatch[] | null> {
  const result = await coreGet<{ batches: CoreBatch[] }>('/api/batches', 'batches')
  return result?.batches ?? null
}

export async function getEvents(runId: string, after = 0): Promise<RuntimeEvent[]> {
  const result = await coreGet<{ events: RuntimeEvent[] }>(
    `/api/runs/${encodeURIComponent(runId)}/events?after=${after}`,
    `events:${runId}:${after}`,
  )
  return result?.events ?? []
}

export function createRun(domain: string, state: Record<string, unknown>): Promise<CoreRun> {
  return corePost<CoreRun>('/api/runs', { domain, state })
}

export function resumeRun(runId: string): Promise<CoreRun> {
  return corePost<CoreRun>(`/api/runs/${encodeURIComponent(runId)}/resume`)
}

export function cancelRun(runId: string): Promise<CoreRun> {
  return corePost<CoreRun>(`/api/runs/${encodeURIComponent(runId)}/cancel`)
}

export function decideApproval(
  id: string,
  action: 'approve' | 'reject' | 'revise',
  response: Record<string, unknown> = {},
): Promise<CoreRun & { decision?: string }> {
  return corePost<CoreRun & { decision?: string }>(
    `/api/approvals/${encodeURIComponent(id)}/${action}`,
    { response },
  )
}

export function createBatch(body: {
  name: string
  workflow: string
  concurrency_limit: number
  items: Record<string, unknown>[]
}): Promise<CoreBatch> {
  return corePost<CoreBatch>('/api/batches', body)
}

export function cancelBatch(id: string): Promise<CoreBatch> {
  return corePost<CoreBatch>(`/api/batches/${encodeURIComponent(id)}/cancel`)
}

/* SSE：断线由浏览器自动重连，Last-Event-ID 由服务端 after 游标保证不重复。 */
export function subscribeRunEvents(
  runId: string,
  after: number,
  handlers: {
    onEvent: (event: RuntimeEvent) => void
    onOpen?: () => void
    onError?: () => void
    onEnd?: () => void
  },
): () => void {
  const source = new EventSource(
    api(`/api/runs/${encodeURIComponent(runId)}/events/stream?after=${after}&token=${encodeURIComponent(token)}`),
  )
  const types = [
    'run_started', 'run_waiting', 'run_completed', 'run_failed', 'run_cancelled',
    'node_started', 'node_progress', 'node_completed', 'node_failed', 'node_retrying',
    'artifact_created', 'approval_required', 'approval_resolved', 'cost_updated', 'batch_updated',
  ]
  types.forEach((type) => {
    source.addEventListener(type, (raw) => {
      try {
        handlers.onEvent(JSON.parse((raw as MessageEvent).data) as RuntimeEvent)
      } catch {
        /* 事件解析失败不阻塞时间线 */
      }
    })
  })
  source.addEventListener('end', () => handlers.onEnd?.())
  source.onopen = () => handlers.onOpen?.()
  source.onerror = () => handlers.onError?.()
  return () => source.close()
}

export async function createConversation(
  interactionMode: 'autonomous' | 'guided' = 'autonomous',
  domain?: string,
): Promise<Conversation> {
  return corePost<Conversation>('/api/conversations', {
    interaction_mode: interactionMode,
    ...(domain ? { domain } : {}),
  })
}

export async function getConversations(domain?: string, query = ''): Promise<Conversation[]> {
  const params = new URLSearchParams()
  if (domain) params.set('domain', domain)
  if (query) params.set('q', query)
  const suffix = params.size ? `?${params.toString()}` : ''
  const result = await coreGet<{ conversations: Conversation[] }>(`/api/conversations${suffix}`, `conversations:${domain ?? 'all'}:${query}`)
  return result?.conversations ?? []
}

export async function renameConversation(id: string, title: string): Promise<Conversation> {
  await ensureToken()
  const response = await fetch(api(`/api/conversations/${encodeURIComponent(id)}`), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', 'X-Studio-Token': token },
    body: JSON.stringify({ title }),
  })
  if (!response.ok) throw await apiFailure(response, '重命名失败')
  return response.json() as Promise<Conversation>
}

export async function setConversationFastDomain(
  id: string, domain: 'comic' | 'commerce' | 'studio' | null,
): Promise<Conversation> {
  await ensureToken()
  const response = await fetch(api(`/api/conversations/${encodeURIComponent(id)}`), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', 'X-Studio-Token': token },
    body: JSON.stringify({ fast_domain: domain }),
  })
  if (!response.ok) throw await apiFailure(response, '切换快捷模式失败')
  return response.json() as Promise<Conversation>
}

export function decideMediaCost(
  conversationId: string, generationRequestId: string, decision: 'approve' | 'reject',
): Promise<MediaJob> {
  return corePost<MediaJob>(
    `/api/conversations/${encodeURIComponent(conversationId)}/media-jobs/${encodeURIComponent(generationRequestId)}/approval`,
    { decision },
  )
}

export async function deleteConversation(id: string): Promise<void> {
  await ensureToken()
  const response = await fetch(api(`/api/conversations/${encodeURIComponent(id)}`), {
    method: 'DELETE', headers: { 'X-Studio-Token': token },
  })
  if (!response.ok) throw await apiFailure(response, '删除聊天失败')
}

export async function getConversation(id: string): Promise<Conversation | null> {
  return coreGet<Conversation>(`/api/conversations/${encodeURIComponent(id)}`, `conversation:${id}`)
}

export type ImageGenerationEventName = 'prompt_prepared' | 'image_generating' | 'image_ready' | 'image_summary' | 'image_failed' | 'cost_approval'
export interface ImageGenerationEvent {
  generation_request_id: string
  message_id?: string
  artifact_id?: string
  width?: number
  height?: number
}

export async function streamConversationMessage(
  id: string,
  content: string,
  domainHint: string | null,
  handlers: {
    onIntent?: (plan: IntentPlan) => void
    onDelta: (content: string) => void
    onMessage?: (message: ConversationMessage) => void
    onRun?: (run: CoreRun) => void
    onActivity?: (activity: { generation_request_id: string; status: string; label: string }) => void
    onPublicActivity?: (activity: { kind: string; label: string; detail: string }) => void
    onImageEvent?: (name: ImageGenerationEventName, payload: ImageGenerationEvent) => void
  },
  dataMode?: 'demo' | 'production',
  enhancePrompt?: boolean,
  generationRequestId?: string,
  executionContext?: { selected_domain: string | null; execution_mode: 'fast' | 'normal' },
): Promise<void> {
  await ensureToken()
  const response = await fetch(api(`/api/conversations/${encodeURIComponent(id)}/messages/stream`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Studio-Token': token },
    body: JSON.stringify({ content, domain_hint: domainHint, data_mode: dataMode, enhance_prompt: enhancePrompt === true, generation_request_id: generationRequestId, ...executionContext }),
  })
  if (!response.ok || !response.body) {
    throw await apiFailure(response, '消息发送失败')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done })
    const packets = buffer.split('\n\n')
    buffer = packets.pop() ?? ''
    for (const packet of packets) {
      const event = packet.match(/^event: (.+)$/m)?.[1]
      const raw = packet.match(/^data: (.+)$/m)?.[1]
      if (!event || !raw) continue
      const payload = JSON.parse(raw) as Record<string, unknown>
      if (event === 'delta') handlers.onDelta(String(payload.content ?? ''))
      else if (event === 'intent') handlers.onIntent?.(payload as unknown as IntentPlan)
      else if (event === 'message') handlers.onMessage?.(payload as unknown as ConversationMessage)
      else if (event === 'run') handlers.onRun?.(payload as unknown as CoreRun)
      else if (event === 'activity') handlers.onActivity?.(payload as { generation_request_id: string; status: string; label: string })
      else if (event === 'public_activity') handlers.onPublicActivity?.(payload as { kind: string; label: string; detail: string })
      else if (['prompt_prepared', 'image_generating', 'image_ready', 'image_summary', 'image_failed', 'cost_approval'].includes(event)) {
        handlers.onImageEvent?.(event as ImageGenerationEventName, payload as unknown as ImageGenerationEvent)
      }
      else if (event === 'error') {
        throw new CoreApiError(
          String(payload.safe_message ?? '操作未完成'), response.status,
          String(payload.trace_id ?? response.headers.get('X-Trace-ID') ?? ''),
          payload.error_id ? String(payload.error_id) : null,
        )
      }
    }
    if (done) break
  }
}
