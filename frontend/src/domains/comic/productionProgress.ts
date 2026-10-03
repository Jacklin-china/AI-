import type { CoreRun } from '../../types'
import type { RuntimeEvent } from '../../services/core'

const imageSteps = ['cost_approval', 'director', 'director_gate', 'storyboard', 'prompt', 'prepare', 'generate', 'qc', 'human_review', 'archive']

/** Progress of real completed production stages, never elapsed-time pseudo progress. */
export function comicProductionProgress(run: CoreRun, events: RuntimeEvent[], imageReady: boolean): { percent: number; label: string; active: boolean; visible: boolean } {
  const latest = new Map<string, string>()
  for (const event of [...events].sort((a, b) => a.sequence - b.sequence)) {
    if (event.node_id && ['node_started', 'node_completed', 'node_failed', 'node_retrying'].includes(event.event_type)) latest.set(event.node_id, event.event_type)
  }
  const completed = imageSteps.filter(step => latest.get(step) === 'node_completed').length
  const finished = run.status === 'completed' && imageReady
  const terminal = ['failed', 'cancelled', 'completed'].includes(run.status)
  return {
    percent: finished ? 100 : Math.min(99, Math.floor(completed / imageSteps.length * 100)),
    label: run.current_node === 'generate' ? '正在生成图片' : run.current_node === 'qc' ? '正在检查画面' : '正在准备生成画面',
    active: !terminal && ['storyboard', 'prompt', 'prepare', 'generate', 'qc', 'archive', 'rework'].includes(run.current_node),
    visible: run.domain === 'comic' && !!run.state.quick_creation && !imageReady &&
      !terminal && ['storyboard', 'prompt', 'prepare', 'generate', 'qc', 'archive', 'rework'].includes(run.current_node),
  }
}
