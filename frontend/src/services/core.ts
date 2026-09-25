import type { Conversation, ConversationMessage, CoreApproval, CoreArtifact, CoreBatch, CoreRun, IntentPlan } from '../types'

export interface RuntimeEvent {
  id: number
  run_id: string
  sequence: number
  event_type: string
  node_id: string | null
  payload: Record<string, unknown>
  created_at: string
}

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '')
  ?? (import.meta.env.DEV ? 'http://127.0.0.1:8000' : '')
let token = document.querySelector<HTMLMetaElement>('meta[name="studio-token"]')?.content ?? ''
let tokenPromise: Promise<string> | null = null

function api(path: string): string { return `${API_BASE}${path}` }

export class CoreApiError extends Error {
  constructor(
    message: string,
    public readonly httpStatus: number,
    public readonly traceId: string | null,
    public readonly errorId: string | null,
  ) {
    super(errorId ? `${message} · 错误编号：${errorId}` : message)
    this.name = 'CoreApiError'
  }
}

async function apiFailure(response: Response, fallback: string): Promise<CoreApiError> {
  const body = await response.json().catch(() => ({})) as {
    error?: string; safe_message?: string; trace_id?: string; error_id?: string
  }
  return new CoreApiError(
    body.safe_message ?? body.error ?? fallback,
    response.status,
    body.trace_id ?? response.headers.get('X-Trace-ID'),
    body.error_id ?? null,
  )
}

async function ensureToken(): Promise<string> {
  if (token && token !== '__TOKEN__') return token
  tokenPromise ??= fetch(api('/api/session')).then(async (response) => {
    if (!response.ok) throw await apiFailure(response, '无法连接 Kantoku 后端')
    const result = await response.json() as { token: string }
    token = result.token
    return token
  })
  return tokenPromise
}

/* 每个数据源只保留最后一次生效响应：新请求取消旧请求，旧响应即使晚到也不会覆盖新状态。 */
const inflight = new Map<string, AbortController>()
const appliedSeq = new Map<string, number>()
let globalSeq = 0

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

async function coreGet<T>(path: string, key: string): Promise<T | null> {
  await ensureToken()
  inflight.get(key)?.abort()
  const controller = new AbortController()
  inflight.set(key, controller)
  const seq = ++globalSeq
  try {
    const response = await fetch(api(path), {
      headers: { 'X-Studio-Token': token },
      signal: controller.signal,
    })
    if (!response.ok) throw await apiFailure(response, '请求没有完成')
    const result = (await response.json()) as T
    if (seq < (appliedSeq.get(key) ?? 0)) return null
    appliedSeq.set(key, seq)
    return result
  } catch (error) {
    if (isAbort(error)) return null
    throw error
  } finally {
    if (inflight.get(key) === controller) inflight.delete(key)
  }
}

async function corePost<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  await ensureToken()
  const response = await fetch(api(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Studio-Token': token },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw await apiFailure(response, '请求没有完成')
  const result = (await response.json()) as T
  return result
}

export function disposeAll(): void {
  inflight.forEach((controller) => controller.abort())
  inflight.clear()
}

export async function getRuns(): Promise<CoreRun[] | null> {
  const result = await coreGet<{ runs: CoreRun[] }>('/api/runs', 'runs')
  return result?.runs ?? null
}

export async function getRun(runId: string): Promise<CoreRun | null> {
  return coreGet<CoreRun>(`/api/runs/${encodeURIComponent(runId)}`, `run:${runId}`)
}

export async function getApprovals(scope = 'approvals'): Promise<CoreApproval[] | null> {
  const result = await coreGet<{ approvals: CoreApproval[] }>('/api/approvals', scope)
  return result?.approvals ?? null
}

export async function getArtifacts(runId?: string): Promise<CoreArtifact[] | null> {
  const path = runId ? `/api/artifacts?run_id=${encodeURIComponent(runId)}` : '/api/artifacts'
  const result = await coreGet<{ artifacts: CoreArtifact[] }>(path, `artifacts:${runId ?? 'all'}`)
  return result?.artifacts ?? null
}

export async function getArtifactContentUrl(artifactId: string): Promise<string | null> {
  await ensureToken()
  const response = await fetch(api(`/api/artifacts/${encodeURIComponent(artifactId)}/content`), {
    headers: { 'X-Studio-Token': token },
  })
  if (!response.ok) return null
  return URL.createObjectURL(await response.blob())
}

export async function getArtifact(artifactId: string): Promise<CoreArtifact | null> {
  return coreGet<CoreArtifact>(`/api/artifacts/${encodeURIComponent(artifactId)}`, `artifact:${artifactId}`)
}

export async function getTaskImageUrl(requestId: string): Promise<string | null> {
  await ensureToken()
  const response = await fetch(api(`/media/${encodeURIComponent(requestId)}`), {
    headers: { 'X-Studio-Token': token },
  })
  if (!response.ok) return null
  return URL.createObjectURL(await response.blob())
}

export async function getBatches(): Promise<CoreBatch[] | null> {
  const result = await coreGet<{ batches: CoreBatch[] }>('/api/batches', 'batches')
  return result?.batches ?? null
}

export async function getEvents(runId: string, after = 0): Promise<RuntimeEvent[]> {
  const result = await coreGet<{ events: RuntimeEvent[] }>(
    `/api/runs/${encodeURIComponent(runId)}/events?after=${after}`,
    `events:${runId}:${after}`,
  )
  return result?.events ?? []
}

export function createRun(domain: string, state: Record<string, unknown>): Promise<CoreRun> {
  return corePost<CoreRun>('/api/runs', { domain, state })
}

export function resumeRun(runId: string): Promise<CoreRun> {
  return corePost<CoreRun>(`/api/runs/${encodeURIComponent(runId)}/resume`)
}

export function cancelRun(runId: string): Promise<CoreRun> {
  return corePost<CoreRun>(`/api/runs/${encodeURIComponent(runId)}/cancel`)
}

export function decideApproval(
  id: string,
  action: 'approve' | 'reject' | 'revise',
  response: Record<string, unknown> = {},
): Promise<CoreRun & { decision?: string }> {
  return corePost<CoreRun & { decision?: string }>(
    `/api/approvals/${encodeURIComponent(id)}/${action}`,
    { response },
  )
}

export function createBatch(body: {
  name: string
  workflow: string
  concurrency_limit: number
  items: Record<string, unknown>[]
}): Promise<CoreBatch> {
  return corePost<CoreBatch>('/api/batches', body)
}

export function cancelBatch(id: string): Promise<CoreBatch> {
  return corePost<CoreBatch>(`/api/batches/${encodeURIComponent(id)}/cancel`)
}

/* SSE：断线由浏览器自动重连，Last-Event-ID 由服务端 after 游标保证不重复。 */
export function subscribeRunEvents(
  runId: string,
  after: number,
  handlers: {
    onEvent: (event: RuntimeEvent) => void
    onOpen?: () => void
    onError?: () => void
    onEnd?: () => void
  },
): () => void {
  const source = new EventSource(
    api(`/api/runs/${encodeURIComponent(runId)}/events/stream?after=${after}&token=${encodeURIComponent(token)}`),
  )
  const types = [
    'run_started', 'run_waiting', 'run_completed', 'run_failed', 'run_cancelled',
    'node_started', 'node_progress', 'node_completed', 'node_failed', 'node_retrying',
    'artifact_created', 'approval_required', 'approval_resolved', 'cost_updated', 'batch_updated',
  ]
  types.forEach((type) => {
    source.addEventListener(type, (raw) => {
      try {
        handlers.onEvent(JSON.parse((raw as MessageEvent).data) as RuntimeEvent)
      } catch {
        /* 事件解析失败不阻塞时间线 */
      }
    })
  })
  source.addEventListener('end', () => handlers.onEnd?.())
  source.onopen = () => handlers.onOpen?.()
  source.onerror = () => handlers.onError?.()
  return () => source.close()
}

export async function createConversation(
  interactionMode: 'autonomous' | 'guided' = 'autonomous',
  domain?: string,
): Promise<Conversation> {
  return corePost<Conversation>('/api/conversations', {
    interaction_mode: interactionMode,
    ...(domain ? { domain } : {}),
  })
}

export async function getConversations(domain?: string, query = ''): Promise<Conversation[]> {
  const params = new URLSearchParams()
  if (domain) params.set('domain', domain)
  if (query) params.set('q', query)
  const suffix = params.size ? `?${params.toString()}` : ''
  const result = await coreGet<{ conversations: Conversation[] }>(`/api/conversations${suffix}`, `conversations:${domain ?? 'all'}:${query}`)
  return result?.conversations ?? []
}

export async function renameConversation(id: string, title: string): Promise<Conversation> {
  await ensureToken()
  const response = await fetch(api(`/api/conversations/${encodeURIComponent(id)}`), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', 'X-Studio-Token': token },
    body: JSON.stringify({ title }),
  })
  if (!response.ok) throw await apiFailure(response, '重命名失败')
  return response.json() as Promise<Conversation>
}

export async function setConversationFastDomain(
  id: string, domain: 'comic' | 'commerce' | 'studio' | null,
): Promise<Conversation> {
  await ensureToken()
  const response = await fetch(api(`/api/conversations/${encodeURIComponent(id)}`), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', 'X-Studio-Token': token },
    body: JSON.stringify({ fast_domain: domain }),
  })
  if (!response.ok) throw await apiFailure(response, '切换快捷模式失败')
  return response.json() as Promise<Conversation>
}

export async function deleteConversation(id: string): Promise<void> {
  await ensureToken()
  const response = await fetch(api(`/api/conversations/${encodeURIComponent(id)}`), {
    method: 'DELETE', headers: { 'X-Studio-Token': token },
  })
  if (!response.ok) throw await apiFailure(response, '删除聊天失败')
}

export async function getConversation(id: string): Promise<Conversation | null> {
  return coreGet<Conversation>(`/api/conversations/${encodeURIComponent(id)}`, `conversation:${id}`)
}

export type ImageGenerationEventName = 'prompt_prepared' | 'image_generating' | 'image_ready' | 'image_summary' | 'image_failed'
export interface ImageGenerationEvent {
  generation_request_id: string
  message_id?: string
  artifact_id?: string
  width?: number
  height?: number
}

export async function streamConversationMessage(
  id: string,
  content: string,
  domainHint: string | null,
  handlers: {
    onIntent?: (plan: IntentPlan) => void
    onDelta: (content: string) => void
    onMessage?: (message: ConversationMessage) => void
    onRun?: (run: CoreRun) => void
    onActivity?: (activity: { generation_request_id: string; status: string; label: string }) => void
    onImageEvent?: (name: ImageGenerationEventName, payload: ImageGenerationEvent) => void
  },
  dataMode?: 'demo' | 'production',
  enhancePrompt?: boolean,
  generationRequestId?: string,
): Promise<void> {
  await ensureToken()
  const response = await fetch(api(`/api/conversations/${encodeURIComponent(id)}/messages/stream`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Studio-Token': token },
    body: JSON.stringify({ content, domain_hint: domainHint, data_mode: dataMode, enhance_prompt: enhancePrompt === true, generation_request_id: generationRequestId }),
  })
  if (!response.ok || !response.body) {
    throw await apiFailure(response, '消息发送失败')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done })
    const packets = buffer.split('\n\n')
    buffer = packets.pop() ?? ''
    for (const packet of packets) {
      const event = packet.match(/^event: (.+)$/m)?.[1]
      const raw = packet.match(/^data: (.+)$/m)?.[1]
      if (!event || !raw) continue
      const payload = JSON.parse(raw) as Record<string, unknown>
      if (event === 'delta') handlers.onDelta(String(payload.content ?? ''))
      else if (event === 'intent') handlers.onIntent?.(payload as unknown as IntentPlan)
      else if (event === 'message') handlers.onMessage?.(payload as unknown as ConversationMessage)
      else if (event === 'run') handlers.onRun?.(payload as unknown as CoreRun)
      else if (event === 'activity') handlers.onActivity?.(payload as { generation_request_id: string; status: string; label: string })
      else if (['prompt_prepared', 'image_generating', 'image_ready', 'image_summary', 'image_failed'].includes(event)) {
        handlers.onImageEvent?.(event as ImageGenerationEventName, payload as unknown as ImageGenerationEvent)
      }
      else if (event === 'error') {
        throw new CoreApiError(
          String(payload.safe_message ?? '操作未完成'), response.status,
          String(payload.trace_id ?? response.headers.get('X-Trace-ID') ?? ''),
          payload.error_id ? String(payload.error_id) : null,
        )
      }
    }
    if (done) break
  }
}
