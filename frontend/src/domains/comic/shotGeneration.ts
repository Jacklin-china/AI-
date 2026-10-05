import type { CoreRun } from '../../types'

/** Project + Shot + immutable request identity; navigation never owns image state. */
export function shotGeneration(runs: CoreRun[], projectId: string, shotId: string): CoreRun | null {
  return runs.filter(run => {
    const creation = run.state.quick_creation as Record<string, unknown> | undefined
    return run.workflow === 'comic.production.v1' && creation?.project_id === projectId && creation?.shot_id === shotId
  }).sort((a, b) => b.started_at.localeCompare(a.started_at))[0] ?? null
}
