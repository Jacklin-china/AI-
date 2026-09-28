// 从真实执行摘要决定显示层级，不在 UI 推算节点成功或生成状态。
export const directorNodeLabels: Record<string, string> = {
  creative_understanding: '创意理解', visual_direction: '视觉导演',
  cinematography: '摄影指导', director_critic: '导演审核', director_assemble: '导演方案',
}
export function visibleDirectorNodes(mode: string, stages: { stage: string }[]): { stage: string }[] {
  return mode === 'professional' ? stages : []
}
export function editableDirectorNode(stage: string): boolean {
  return ['creative_understanding', 'visual_direction', 'cinematography'].includes(stage)
}
export function selectDirectorExecution<T extends { run_id: string }>(
  executions: T[], selectedRun: string, submitting: boolean, previousRunIds: string[],
): T | undefined {
  // 新请求执行时不沿用旧任务的“已完成”，等待后端出现真正的新 Run。
  if (submitting) return executions.find(item => !previousRunIds.includes(item.run_id))
  return executions.find(item => item.run_id === selectedRun) ?? executions[0]
}
export const directorFieldLabels: Record<string, string> = {
  intent_summary: '创作意图', story_context: '故事情境', emotional_target: '情绪目标',
  audience_experience: '观众感受', narrative_focus: '叙事重点', hard_constraints: '用户硬约束',
  soft_preferences: '偏好', creative_freedom: '创作空间', unresolved_questions: '尚未确定',
  visual_strategy: '视觉策略', visual_focus: '视觉焦点', subject_environment_relation: '人物与环境',
  composition_strategy: '构图策略', color_strategy: '色彩策略', continuity_rules: '连续性要求',
  creative_choices: '创作理由', risk_flags: '风险提示', shot_size: '景别', camera_angle: '机位',
  camera_distance: '镜头距离', spatial_feel: '空间关系', lens_or_spatial_feel: '镜头空间感',
  movement: '运镜', lighting: '光影', light_source: '光源', light_direction: '光源方向',
  color_relationship: '色彩关系', depth_strategy: '景深', material_language: '材质',
}

export const directorStateLabels: Record<string, string> = {
  pending: '未开始', running: '执行中', completed: '已完成', waiting: '需要审核',
  needs_review: '需要审核', failed: '失败', stale: '已过期', cancelled: '已取消',
}
export function publicDirectorSections(spec: Record<string, unknown> | null): { title: string; fields: Record<string, unknown> }[] {
  if (!spec) return []
  // Strict public v2 projection: never render legacy fields or arbitrary model metadata.
  return [['creative_decision', '创作理解'], ['director_plan', '导演方案'], ['cinematography', '摄影方案']]
    .flatMap(([key, title]) => {
      const body = spec[key!]
      if (!body || typeof body !== 'object' || Array.isArray(body)) return []
      return [{ title: title!, fields: Object.fromEntries(Object.entries(body).filter(([field]) => field in directorFieldLabels)) }]
    })
}
export function directorSummary(spec: Record<string, unknown> | null): string {
  return publicDirectorSections(spec).map(section => {
    const values = Object.values(section.fields).filter(value => typeof value === 'string' && value)
    return `**${section.title}**\n\n${values.slice(0, 2).join('；')}`
  }).join('\n\n')
}
export function directorIsStale(
  spec: Record<string, unknown> | null, briefVersion?: number, directorVersion?: number | null,
  assets: { asset_id: string; version: number; pinned_version: number | null; state: string }[] = [],
): boolean {
  if (!spec) return false
  if (briefVersion && spec.creative_brief_version !== briefVersion) return true
  if (directorVersion && spec.version !== directorVersion) return true
  const refs = spec.asset_versions as Record<string, number> | undefined
  return Object.entries(refs ?? {}).some(([id, version]) => {
    const asset = assets.find(item => item.asset_id === id.replace(/^asset:/, ''))
    return !asset || asset.state === 'deleted' || (asset.pinned_version ?? asset.version) !== version
  })
}
export function draftConfirmationKey(projectId: string, spec: Record<string, unknown> | null): string {
  if (!spec || spec.schema_version !== 2) return ''
  return JSON.stringify([projectId, spec.spec_id, spec.version, spec.creative_brief_version,
    spec.asset_versions, spec.storyboard_version, spec.shot_version,
    publicDirectorSections(spec), spec.critic_result])
}
export function canConfirmDirector(status: string | undefined, spec: Record<string, unknown> | null, stale: boolean, dirty: boolean): boolean {
  return status === 'completed' && spec?.schema_version === 2 && !stale && !dirty &&
    publicDirectorSections(spec).length === 3 && (spec.critic_result as { verdict?: string } | null)?.verdict === 'pass'
}
export function stageDraftKey(projectId: string, runId: string, stage: string): string {
  return JSON.stringify([projectId, runId, stage])
}
export function chronologicalDirectorExecutions<T extends { run_id: string }>(
  executions: T[], records: Record<string, { started_at: string }>,
): T[] {
  // A task summary may arrive before its persisted input. Never insert an orphan
  // assistant turn at the top; the pending user bubble remains until Run hydration.
  return executions.filter(item => !!records[item.run_id]).sort((a, b) =>
    records[a.run_id]!.started_at.localeCompare(records[b.run_id]!.started_at))
}
