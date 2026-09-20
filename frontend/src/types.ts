export type TaskStatus = 'succeeded' | 'failed' | 'unknown' | 'draft'
export type JobState = 'idle' | 'running' | 'done' | 'error'

export interface StudioTask {
  request_id: string
  project: string
  prompt: string
  shot_no: number
  estimate_fen: number
  status: TaskStatus
  has_image: boolean
  actual_fen: number | null
  ledger_status: string
  error: string | null
  created_at: string
  qc: QcResult | null
  review: HumanReview | null
  rework: ReworkItem | null
  archived: boolean
}

export interface StudioConfig {
  image: string
  chat: string
  vision: string
  size: string
  limit: string
  estimate_fen?: number
}

export interface StudioBudget {
  project: string
  task_count: number
  settled_fen: number
  held_fen: number
  unknown_count: number
  unbilled_count: number
  limit_fen: number | null
  available_fen: number | null
}

export interface QcResult {
  broken_hands: boolean
  watermark: boolean
  composition_ok: boolean
  persona_consistency: number
  reason: string
  confidence: number
}

export interface HumanReview {
  approved: boolean
  failure_reasons: string[]
}

export interface ReworkItem {
  status: 'pending' | 'approved' | 'cancelled'
  target_request_id: string | null
}

export interface JobResult {
  prompt?: string
  request_id?: string
  qc?: QcResult
  message?: string
  approved?: boolean
  decision?: 'approve' | 'reject' | 'request_revision'
  rework_created?: boolean
  settled_fen?: number
  held_fen?: number
  available_fen?: number
}

export interface StudioJob {
  state: JobState
  id?: string
  action?: string
  error?: string
  result?: JobResult
}

export interface StudioState {
  tasks: StudioTask[]
  budgets: Record<string, StudioBudget>
  job: StudioJob
  config: StudioConfig
}

export interface StudioForm {
  project: string
  shot_no: number
  purpose: string
  subject: string
  style: string
  audience: string
  prompt: string
  price: string
}

export interface GenerationSettings {
  model: string
  ratio: string
  resolution: string
  quality: string
  quantity: number
}

export type ExecutionStatus = 'pending' | 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled'
export type BatchStatus = 'pending' | 'running' | 'waiting' | 'completed' | 'partial_failed' | 'cancelled'

export interface NodeExecution {
  run_id: string
  node_id: string
  status: ExecutionStatus
  started_at: string | null
  completed_at: string | null
  retry_count: number
  error: string | null
  outputs: Record<string, unknown>
}

export interface CoreRun {
  id: string
  domain: string
  workflow: string
  status: ExecutionStatus
  state: Record<string, unknown>
  current_node: string
  started_at: string
  updated_at: string
  completed_at: string | null
  error: string | null
  cost_fen: number
  nodes: NodeExecution[]
}

export interface CoreApproval {
  id: string
  run_id: string
  node_id: string
  decision: 'pending' | 'approve' | 'reject' | 'request_revision'
  request: Record<string, unknown>
  response: Record<string, unknown>
  created_at: string
  decided_at: string | null
}

export interface CoreArtifact {
  id: string
  type: 'image' | 'video' | 'prompt' | 'document' | 'json' | 'listing' | 'report'
  run_id: string
  node_id: string
  source: string
  status: string
  created_at: string
  metadata: Record<string, unknown>
  location: string | null
  version: number
}

export interface CoreSkill {
  id: string
  name: string
  domain: string
  description: string
  version: string
  required_tools: string[]
  input_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
  handler_ref: string | null
}

export interface CoreBatch {
  id: string
  name: string
  status: BatchStatus
  concurrency_limit: number
  created_at: string
  updated_at: string
  run_ids: string[]
  runs: CoreRun[]
}
