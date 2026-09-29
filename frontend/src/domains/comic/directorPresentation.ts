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
  intent_summary: '创作意图', narrative_context: '故事情境', emotional_target: '情绪目标',
  audience_experience: '观众感受', narrative_focus: '叙事重点', hard_constraints: '用户硬约束',
  soft_preferences: '偏好', creative_freedom: '创作空间', unresolved_questions: '尚未确定',
  visual_strategy: '视觉策略', visual_focus: '视觉焦点', subject_environment_relation: '人物与环境',
  composition_strategy: '构图策略', color_strategy: '色彩策略', continuity_rules: '连续性要求',
  creative_choices: '创作理由', risk_flags: '风险提示', shot_size: '景别', camera_angle: '机位',
  camera_distance: '镜头距离', spatial_feel: '空间关系', lens_or_spatial_feel: '镜头空间感',
  movement: '运镜', lighting: '光影', light_source: '光源', light_direction: '光源方向',
  color_relationship: '色彩关系', depth_strategy: '景深', material_language: '材质',
  camera_language: '摄影表达', style_boundary: '风格边界', character_expression: '人物表情',
  character_pose: '人物姿态', character_presence: '人物表现',
  public_decision: '摄影说明', creative_reason: '摄影理由',
}

export const directorStageSections: Record<string, string> = {
  creative_understanding: 'creative_decision', visual_direction: 'director_plan', cinematography: 'cinematography',
}
export const fastDirectorNodeLabels: Record<string, string> = {
  creative_understanding: '故事理解', visual_direction: '视觉方向', cinematography: '镜头感觉', director_assemble: '整体方案',
}
const fastDirectorFields: Record<string, Record<string, string>> = {
  creative_decision: { intent_summary: '你想表达的内容', narrative_context: '故事背景', emotional_target: '氛围', hard_constraints: '必须保留' },
  director_plan: { visual_strategy: '整体方向', visual_focus: '画面重点', subject_environment_relation: '人物和环境', composition_strategy: '画面安排', color_strategy: '色彩感觉' },
  cinematography: { public_decision: '镜头建议', shot_size: '画面范围', spatial_feel: '画面空间' },
}
export function directorPageFields(section: string, body: Record<string, unknown>, mode = 'professional'): Record<string, unknown> {
  const labels = mode === 'fast' ? fastDirectorFields[section] ?? {} : directorFieldLabels
  return Object.fromEntries(Object.entries(body).filter(([key, value]) => key in labels && value != null))
}
export function directorPageFieldLabel(section: string, field: string, mode = 'professional'): string {
  return (mode === 'fast' ? fastDirectorFields[section]?.[field] : directorFieldLabels[field]) ?? directorFieldLabels[field] ?? field
}

export const directorStateLabels: Record<string, string> = {
  pending: '未开始', running: '执行中', completed: '已完成', waiting: '需要审核',
  needs_review: '需要审核', failed: '失败', stale: '已过期', cancelled: '已取消',
  needs_revision: '待调整', missing: '待补充', complete: '方案完整',
}
export function publicDirectorSections(spec: Record<string, unknown> | null): { title: string; fields: Record<string, unknown> }[] {
  if (!spec) return []
  // Strict public v2 projection: never render legacy fields or arbitrary model metadata.
  return [['creative_decision', '创作理解'], ['director_plan', '导演方案'], ['cinematography', '摄影方案']]
    .flatMap(([key, title]) => {
      const body = spec[key!]
      if (!body || typeof body !== 'object' || Array.isArray(body)) return []
      return [{ title: title!, fields: Object.fromEntries(Object.entries(body).filter(([field, value]) => field in directorFieldLabels && value != null)) }]
    })
}
export function directorSummary(spec: Record<string, unknown> | null, mode = 'professional'): string {
  return publicDirectorSections(spec).filter(section => mode !== 'fast' || section.title !== '摄影方案').map(section => {
    const values = Object.values(section.fields).filter(value => typeof value === 'string' && value)
    return `**${section.title}**\n\n${values.slice(0, 2).join('；')}`
  }).join('\n\n')
}
export function directorConversationSummary(spec: Record<string, unknown> | null): string {
  if (spec?.schema_version !== 2) return ''
  const understanding = spec.creative_decision as Record<string, unknown> | undefined
  if (typeof understanding?.intent_summary !== 'string' || !understanding.intent_summary.trim()) return ''
  // One concise entry into the workspace, not a second copy of the director report.
  const mood = typeof understanding.emotional_target === 'string' && understanding.emotional_target.trim()
    ? `\n\n氛围：${understanding.emotional_target}` : ''
  const state = typeof spec.version === 'number' ? '导演方案草稿已保存，可以进入工作区查看和修改。' : '已整理导演草稿，尚未保存为正式版本。'
  return `**我理解你的创意**\n\n${understanding.intent_summary}${mood}\n\n${state}`
}
export function workspaceProjectTitle(title: string | undefined, request: string | undefined): string {
  // 自动生成的原话标题已在对话中出现，不再把长需求复制到工具栏。
  return !title || request?.startsWith(title) ? '漫剧作品' : title
}
export function canDispatchDirectorInput(state: { busy: boolean; running: boolean; loading: boolean; error: boolean; cancelling: boolean }): boolean {
  return !Object.values(state).some(Boolean)
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

// 编辑只投影公开决策叶子，绑定身份、用户硬约束和审核结论保持只读。
export function directorDraftFields(spec: Record<string, unknown> | null): Record<string, string> {
  if (spec?.schema_version !== 2) return {}
  return Object.fromEntries(['creative_decision', 'director_plan', 'cinematography'].flatMap(section =>
    Object.entries(spec[section] as Record<string, unknown> ?? {})
      .filter(([key, value]) => key !== 'hard_constraints' && key in directorFieldLabels &&
        (value == null || typeof value === 'string' || Array.isArray(value)))
      .map(([key, value]) => [`${section}.${key}`, Array.isArray(value) ? value.join('\n') : String(value ?? '')]),
  ))
}
export function discardDirectorNodeDraft(fields: Record<string, string>, stage: string): Record<string, string> {
  const prefix = `${directorStageSections[stage]}.`
  return Object.fromEntries(Object.entries(fields).filter(([key]) => !key.startsWith(prefix)))
}
export function editableDirectorDraft(spec: Record<string, unknown>, fields: Record<string, string> = {}): Record<string, unknown> {
  const keys = ['schema_version', 'visual_direction', 'storytelling_goal', 'camera_language', 'composition',
    'lighting', 'color_language', 'emotion', 'character_focus', 'constraints', 'creative_choices',
    'creative_decision', 'director_plan', 'cinematography', 'knowledge_refs', 'asset_versions', 'storyboard_version', 'shot_version']
  // API 数据是 JSON；Vue 的嵌套 Proxy 不能直接 structuredClone。
  const draft = JSON.parse(JSON.stringify(Object.fromEntries(keys.filter(key => key in spec).map(key => [key, spec[key]])))) as Record<string, unknown>
  const allowed = directorDraftFields(spec)
  Object.entries(fields).forEach(([path, text]) => {
    if (!(path in allowed)) throw new Error('不能编辑身份、硬约束或审核结论')
    const [section, key] = path.split('.') as [string, string]
    const body = draft[section] as Record<string, unknown>
    body[key] = Array.isArray(body[key]) ? text.split('\n').map(item => item.trim()).filter(Boolean) : body[key] == null && !text.trim() ? null : text
  })
  return { ...draft, critic_result: null }
}
