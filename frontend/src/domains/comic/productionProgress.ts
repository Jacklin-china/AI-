import type { CoreRun } from '../../types'
import type { RuntimeEvent } from '../../services/core'

const imageSteps = ['cost_approval', 'director', 'director_gate', 'storyboard', 'prompt', 'prepare', 'generate', 'qc', 'human_review', 'archive']

export function isComicFastImage(run: CoreRun): boolean {
  return run.domain === 'comic' && run.state.execution_mode === 'fast' &&
    (run.state.quick_creation as Record<string, unknown> | undefined)?.auto_create_image === true
}

/** Progress of real completed production stages, never elapsed-time pseudo progress. */
export function comicProductionProgress(run: CoreRun, events: RuntimeEvent[], imageReady: boolean): { percent: number; label: string; active: boolean; visible: boolean } {
  const latest = new Map<string, string>()
  for (const event of [...events].sort((a, b) => a.sequence - b.sequence)) {
    if (event.node_id && ['node_started', 'node_completed', 'node_failed', 'node_retrying'].includes(event.event_type)) latest.set(event.node_id, event.event_type)
  }
  const completed = imageSteps.filter(step => latest.get(step) === 'node_completed').length
  const finished = run.status === 'completed' && imageReady
  const terminal = ['failed', 'cancelled', 'completed'].includes(run.status)
  const stages = ['storyboard', 'prompt', 'prepare', 'generate', 'qc', 'archive', 'rework']
  if (isComicFastImage(run)) stages.push('director', 'director_gate')
  const active = !terminal && stages.includes(run.current_node) &&
    !(run.status === 'waiting' && ['director', 'director_gate'].includes(run.current_node))
  const labels: Record<string, string> = {
    director: '正在分析需求', director_gate: '正在校验方案',
    storyboard: '正在优化画面', prompt: '正在优化提示词', prepare: '正在准备图片',
    generate: '正在生成图片', qc: '正在检查画面', archive: '正在保存图片', rework: '正在修改图片',
  }
  return {
    percent: finished ? 100 : Math.min(99, Math.floor(completed / imageSteps.length * 100)),
    label: labels[run.current_node] ?? '正在准备生成画面',
    active,
    visible: run.domain === 'comic' && !!run.state.quick_creation && !imageReady &&
      active,
  }
}
