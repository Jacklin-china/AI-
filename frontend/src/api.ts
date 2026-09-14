import type { StudioState } from './types'

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
