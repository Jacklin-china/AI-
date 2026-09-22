import { ref } from 'vue'

export type RouteName =
  | 'home'
  | 'workspace'
  | 'workspace_run'
  | 'tasks'
  | 'task_run'
  | 'task_batch'
  | 'assets'
  | 'settings'

export interface Route {
  name: RouteName
  domain?: string
  runId?: string
  batchId?: string
  tab?: string
}

export const route = ref<Route>(parse(window.location.pathname))

export function parse(path: string): Route {
  const clean = path.replace(/\/+$/, '') || '/'
  const parts = clean.split('/').filter(Boolean)
  if (parts.length === 0) return { name: 'home' }

  // 旧路由兼容，避免历史链接失效
  if (parts[0] === 'domain' && parts[1]) return { name: 'workspace', domain: parts[1] }
  if (parts[0] === 'runs') {
    return parts[1]
      ? { name: 'task_run', runId: decodeURIComponent(parts[1]) }
      : { name: 'tasks', tab: 'running' }
  }
  if (parts[0] === 'approvals') return { name: 'tasks', tab: 'waiting' }
  if (parts[0] === 'batches') {
    return parts[1]
      ? { name: 'task_batch', batchId: decodeURIComponent(parts[1]) }
      : { name: 'tasks', tab: 'batches' }
  }
  if (parts[0] === 'skills') return { name: 'settings' }
  if (parts[0] === 'history') return { name: 'tasks', tab: 'history' }

  if (parts[0] === 'workspace') {
    const domain = parts[1] ?? 'studio'
    if (parts[2] === 'run' && parts[3]) {
      return { name: 'workspace_run', domain, runId: decodeURIComponent(parts[3]) }
    }
    return { name: 'workspace', domain }
  }
  if (parts[0] === 'tasks') {
    if (parts[1] === 'run' && parts[2]) {
      return { name: 'task_run', runId: decodeURIComponent(parts[2]) }
    }
    if (parts[1] === 'batch' && parts[2]) {
      return { name: 'task_batch', batchId: decodeURIComponent(parts[2]) }
    }
    return { name: 'tasks', tab: parts[1] }
  }
  if (parts[0] === 'assets') return { name: 'assets' }
  if (parts[0] === 'settings') return { name: 'settings' }
  return { name: 'home' }
}

export function href(next: Route): string {
  switch (next.name) {
    case 'home':
      return '/'
    case 'workspace':
      return `/workspace/${next.domain ?? 'studio'}`
    case 'workspace_run':
      return `/workspace/${next.domain ?? 'studio'}/run/${encodeURIComponent(next.runId ?? '')}`
    case 'task_run':
      return `/tasks/run/${encodeURIComponent(next.runId ?? '')}`
    case 'task_batch':
      return `/tasks/batch/${encodeURIComponent(next.batchId ?? '')}`
    case 'tasks':
      return next.tab && next.tab !== 'running' ? `/tasks/${next.tab}` : '/tasks'
    case 'assets':
      return '/assets'
    default:
      return '/settings'
  }
}

export function navigate(next: Route): void {
  const path = href(next)
  if (window.location.pathname !== path) window.history.pushState({}, '', path)
  route.value = { ...next }
  window.scrollTo({ top: 0 })
}

export function openRun(domain: string, runId: string): void {
  navigate({ name: 'workspace_run', domain, runId })
}

window.addEventListener('popstate', () => {
  route.value = parse(window.location.pathname)
})
