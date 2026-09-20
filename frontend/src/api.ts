import type { CoreApproval, CoreArtifact, CoreBatch, CoreRun, CoreSkill, StudioState } from './types'

const token = document.querySelector<HTMLMetaElement>('meta[name="studio-token"]')?.content ?? ''

async function parseResponse<T>(response: Response): Promise<T> {
  const result = (await response.json()) as T & { error?: string }
  if (!response.ok) {
    throw new Error(result.error ?? '请求没有完成')
  }
  return result
}

export async function getState(): Promise<StudioState> {
  const response = await fetch('/api/state', {
    headers: { 'X-Studio-Token': token },
  })
  return parseResponse<StudioState>(response)
}

export async function startAction(action: string, body: Record<string, unknown>): Promise<void> {
  const response = await fetch(`/api/${action}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Studio-Token': token,
    },
    body: JSON.stringify(body),
  })
  await parseResponse<{ accepted: boolean }>(response)
}

export async function loadArtwork(requestId: string): Promise<string> {
  const response = await fetch(`/media/${encodeURIComponent(requestId)}`, {
    headers: { 'X-Studio-Token': token },
  })
  if (!response.ok) {
    throw new Error('原图读取失败')
  }
  return URL.createObjectURL(await response.blob())
}

async function coreGet<T>(path: string): Promise<T> {
  return parseResponse<T>(await fetch(path, { headers: { 'X-Studio-Token': token } }))
}

async function corePost<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  return parseResponse<T>(await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Studio-Token': token },
    body: JSON.stringify(body),
  }))
}

export async function getCoreRuns(): Promise<CoreRun[]> { return (await coreGet<{ runs: CoreRun[] }>('/api/runs')).runs }
export async function getCoreApprovals(): Promise<CoreApproval[]> { return (await coreGet<{ approvals: CoreApproval[] }>('/api/approvals')).approvals }
export async function getCoreArtifacts(): Promise<CoreArtifact[]> { return (await coreGet<{ artifacts: CoreArtifact[] }>('/api/artifacts')).artifacts }
export async function getCoreSkills(): Promise<CoreSkill[]> { return (await coreGet<{ skills: CoreSkill[] }>('/api/skills')).skills }
export async function getCoreBatches(): Promise<CoreBatch[]> { return (await coreGet<{ batches: CoreBatch[] }>('/api/batches')).batches }
export async function createCoreBatch(body: { name: string; workflow: string; concurrency_limit: number; items: Record<string, unknown>[] }): Promise<CoreBatch> { return corePost<CoreBatch>('/api/batches', body) }
export async function decideCoreApproval(id: string, action: 'approve' | 'reject' | 'revise', response: Record<string, unknown> = {}): Promise<CoreRun> { return corePost<CoreRun>(`/api/approvals/${encodeURIComponent(id)}/${action}`, { response }) }
export async function cancelCoreRun(id: string): Promise<CoreRun> { return corePost<CoreRun>(`/api/runs/${encodeURIComponent(id)}/cancel`) }
export async function cancelCoreBatch(id: string): Promise<CoreBatch> { return corePost<CoreBatch>(`/api/batches/${encodeURIComponent(id)}/cancel`) }
