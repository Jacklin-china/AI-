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
