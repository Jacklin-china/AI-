import type { CoreArtifact, CoreRun } from '../../types'

export interface ComicPhase {
  id: string
  label: string
  status: 'pending' | 'running' | 'waiting' | 'completed' | 'failed'
}

/**
 * Product stages are intentionally broader than the legacy runtime nodes.
 * A stage remains pending until a real event/node can prove that it happened;
 * the workspace must not imply that a Director/Asset/Storyboard step exists
 * merely because the old single-shot workflow has started.
 */
const phaseDefinitions = [
  { id: 'brief', label: '创意理解', nodes: [], completedBy: null },
  { id: 'director', label: '导演分析', nodes: [], completedBy: null },
  { id: 'assets', label: '资产', nodes: [], completedBy: null },
  { id: 'storyboard', label: '分镜', nodes: [], completedBy: null },
  { id: 'prompt', label: 'Prompt', nodes: [], completedBy: null },
  { id: 'generate', label: '生图', nodes: ['generate'], completedBy: 'generate' },
  { id: 'qc', label: 'QC', nodes: ['qc'], completedBy: 'qc' },
  { id: 'video', label: '视频', nodes: ['video'], completedBy: 'video' },
] as const

/** Only executed runtime nodes can advance the strip; no storyboard/asset stages are invented. */
export function comicPhases(run: CoreRun | null): ComicPhase[] {
  return phaseDefinitions.map((phase) => {
    if (!run) return { id: phase.id, label: phase.label, status: 'pending' as const }
    const executions = run.nodes.filter((node) => (phase.nodes as readonly string[]).includes(node.node_id))
    const active = (phase.nodes as readonly string[]).includes(run.current_node)
    let status: ComicPhase['status'] = 'pending'
    if (executions.some((node) => node.status === 'failed')) status = 'failed'
    else if (active && run.status === 'waiting') status = 'waiting'
    else if (active && run.status === 'running') status = 'running'
    else if (executions.some((node) => node.node_id === phase.completedBy && node.status === 'completed')) status = 'completed'
    if (phase.id === 'video' && run.status === 'completed' && executions.length) status = 'completed'
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
