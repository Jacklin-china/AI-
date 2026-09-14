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
}

export interface StudioConfig {
  image: string
  chat: string
  vision: string
  size: string
  limit: string
}

export interface QcResult {
  reason: string
  confidence: number
}

export interface JobResult {
  prompt?: string
  request_id?: string
  qc?: QcResult
  message?: string
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
