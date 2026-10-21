/** 阶段导航的共享类型：Workflow 只作为导航呈现，不再常驻一列。 */
export type StageStatus = 'pending' | 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled'
export interface StageItem { id: string; label: string; status: StageStatus; detail?: string }
