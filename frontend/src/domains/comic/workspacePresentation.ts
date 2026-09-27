import type { CoreArtifact, CoreRun } from '../../types'

export interface ComicPhase {
  id: string
  label: string
  status: 'pending' | 'running' | 'waiting' | 'completed' | 'failed'
}

const phaseDefinitions = [
  { id: 'prepare', label: '准备', nodes: ['prepare'], completedBy: 'prepare' },
  { id: 'cost', label: '费用', nodes: ['cost_approval'], completedBy: 'cost_approval' },
  { id: 'generate', label: '生成', nodes: ['generate'], completedBy: 'generate' },
  { id: 'qc', label: '检查', nodes: ['qc'], completedBy: 'qc' },
  { id: 'review', label: '审核', nodes: ['human_review', 'rework'], completedBy: 'human_review' },
  { id: 'delivery', label: '交付', nodes: ['archive', 'video'], completedBy: 'archive' },
] as const

/** Only executed runtime nodes can advance the strip; no storyboard/asset stages are invented. */
export function comicPhases(run: CoreRun | null): ComicPhase[] {
  if (!run) return []
  return phaseDefinitions.map((phase) => {
    const executions = run.nodes.filter((node) => (phase.nodes as readonly string[]).includes(node.node_id))
    const active = (phase.nodes as readonly string[]).includes(run.current_node)
    let status: ComicPhase['status'] = 'pending'
    if (executions.some((node) => node.status === 'failed')) status = 'failed'
    else if (active && run.status === 'waiting') status = 'waiting'
    else if (active && run.status === 'running') status = 'running'
    else if (executions.some((node) => node.node_id === phase.completedBy && node.status === 'completed')) status = 'completed'
    if (phase.id === 'delivery' && run.status === 'completed') status = 'completed'
    return { id: phase.id, label: phase.label, status }
  })
}

/** A historical archive may point to missing content while its original task image still exists. */
export async function resolveComicPreviewUrl(
  artifact: Pick<CoreArtifact, 'id' | 'type'> | null,
  requestId: string | null,
  loadArtifact: (id: string) => Promise<string | null>,
  loadTaskImage: (id: string) => Promise<string | null>,
): Promise<string | null> {
  const artifactUrl = artifact ? await loadArtifact(artifact.id).catch(() => null) : null
  if (artifactUrl) return artifactUrl
  if (!requestId || (artifact && artifact.type !== 'image')) return null
  return loadTaskImage(requestId).catch(() => null)
}
