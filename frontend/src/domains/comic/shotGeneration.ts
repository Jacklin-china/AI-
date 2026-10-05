import type { CoreRun } from '../../types'

/** Project + Shot + immutable request identity; navigation never owns image state. */
export function shotGeneration(runs: CoreRun[], projectId: string, shotId: string): CoreRun | null {
  return runs.filter(run => {
    const creation = run.state.quick_creation as Record<string, unknown> | undefined
    return run.workflow === 'comic.production.v1' && creation?.project_id === projectId && creation?.shot_id === shotId
  }).sort((a, b) => b.started_at.localeCompare(a.started_at))[0] ?? null
}

/** Media preparation is derived from immutable Prompt provenance, not Shot.status. */
export function externalShotPrompt(prompts: Record<string, unknown>[], shotVersion: number, directorVersion: number): Record<string, unknown> | null {
  return prompts.find(prompt => prompt.model_target === 'external' && !!prompt.artifact_id
    && prompt.shot_version === shotVersion && prompt.director_spec_version === directorVersion) ?? null
}

/** Clipboard/export is the saved complete Image Prompt, including negative constraints. */
export function finalImagePrompt(prompt: Record<string, unknown>): string {
  if (typeof prompt.final_prompt === 'string' && prompt.final_prompt.trim()) return prompt.final_prompt
  const negative = String(prompt.negative_prompt ?? '').trim()
  return String(prompt.positive_prompt ?? '') + (negative ? `\n\nNegative Constraint / 禁止内容\n${negative}` : '')
}

export function promptVersionDiff(before: string, after: string): { before: string; after: string }[] {
  const left = before.split('\n'), right = after.split('\n')
  return Array.from({ length: Math.max(left.length, right.length) }, (_, index) => ({
    before: left[index] ?? '', after: right[index] ?? '',
  })).filter(line => line.before !== line.after)
}
