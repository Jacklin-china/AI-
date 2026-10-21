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
  workspacePage?: string
  directorStage?: string
  conversationId?: string
  directorRunId?: string
  workId?: string
  taskId?: string
  creationMode?: 'fast' | 'professional'
}

export const directorStagePaths: Record<string, string> = {
  creative_understanding: 'understanding', visual_direction: 'visual-direction',
  cinematography: 'cinematography', director_critic: 'critic', director_assemble: 'plan',
}
/* 漫剧七步制作导航 + 三个辅助入口（design-spec §13）。
   01 画面方案 → 02 生图指令 → 03 图片优化 → 04 改图指令 → 05 版本比较 → 06 故事编排 → 07 导出材料 → 08 视频生成。
   旧的 director / storyboard 值保留为兼容别名，历史链接不会 404。 */
export const workspacePages = ['conversation', 'plan', 'prompt', 'images', 'optimize', 'revision', 'compare', 'story', 'export', 'video', 'assets', 'history']
/** 旧 page → 新 page 的别名映射：只做归一化，不丢信息。 */
export const legacyPageAliases: Record<string, string> = { director: 'plan', storyboard: 'story' }
function normalizePage(page: string | undefined): string | undefined {
  if (!page) return undefined
  return workspacePages.includes(page) ? page : legacyPageAliases[page]
}
function workspaceSuffix(next: Route): string {
  const page = normalizePage(next.workspacePage)
  if (!page) return ''
  return `/${page}${page === 'plan' ? `/${directorStagePaths[next.directorStage ?? 'director_assemble'] ?? 'plan'}` : ''}`
}

export const route = ref<Route>(parse(window.location.pathname + (window.location.search ?? '')))

const lastWorkspaces = new Map<string, Route>()
function rememberWorkspace(next: Route): void {
  if (!['workspace', 'workspace_run'].includes(next.name)
    || !(next.workId || next.conversationId || next.runId)) return
  const domain = next.domain ?? 'comic'
  lastWorkspaces.set(domain, { ...next })
  try { window.sessionStorage.setItem(`kantoku-workspace-route:${domain}`, href(next)) }
  catch { /* Navigation still works when browser storage is unavailable. */ }
}

/** Global navigation resumes a selection; explicit creation stays in the workspace. */
export function workspaceEntry(domain = 'comic'): Route {
  const remembered = lastWorkspaces.get(domain)
  if (remembered) return { ...remembered }
  try {
    const saved = window.sessionStorage.getItem(`kantoku-workspace-route:${domain}`)
    if (saved) {
      const next = parse(saved)
      if (['workspace', 'workspace_run'].includes(next.name) && next.domain === domain
        && (next.workId || next.conversationId || next.runId)) return next
    }
  } catch { /* A missing or invalid saved location falls back to the read-only work list. */ }
  return { name: 'workspace', domain }
}

export function parse(path: string): Route {
  if (path.includes('?')) {
    const [pathname, query] = path.split('?')
    const conversationId = new URLSearchParams(query).get('conversation')
    const directorRunId = new URLSearchParams(query).get('director_run')
    const workId = new URLSearchParams(query).get('work')
    const taskId = new URLSearchParams(query).get('task')
    const mode = new URLSearchParams(query).get('mode')
    return { ...parse(pathname!), ...(conversationId ? { conversationId } : {}),
      ...(directorRunId ? { directorRunId } : {}),
      ...(workId ? { workId } : {}),
      ...(taskId ? { taskId } : {}),
      ...(mode === 'fast' || mode === 'professional' ? { creationMode: mode } : {}) }
  }
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
    /* 默认领域对齐侧栏「工作区」：漫剧制作是唯一对外提供的创作领域，
       因此 /workspace 与 /workspace/comic 等价，不会落回通用创作工作台。 */
    const domain = parts[1] ?? 'comic'
    const offset = parts[2] === 'run' && parts[3] ? 4 : 2
    const rawPage = parts[offset]
    const node = parts[offset + 1]
    // 旧 page 值（director / storyboard）在此归一化为新七步值，directorStage 继续透传给 01 画面方案。
    const page = normalizePage(rawPage)
    const workspace = page ? {
      workspacePage: page,
      ...(page === 'plan' ? { directorStage: Object.keys(directorStagePaths).find(stage => directorStagePaths[stage] === node) ?? 'director_assemble' } : {}),
    } : {}
    if (parts[2] === 'run' && parts[3]) {
      return { name: 'workspace_run', domain, runId: decodeURIComponent(parts[3]), ...workspace }
    }
    return { name: 'workspace', domain, ...workspace }
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
  const query = new URLSearchParams()
  if (next.conversationId) query.set('conversation', next.conversationId)
  if (next.directorRunId) query.set('director_run', next.directorRunId)
  if (next.workId) query.set('work', next.workId)
  if (next.taskId) query.set('task', next.taskId)
  if (next.creationMode) query.set('mode', next.creationMode)
  return hrefPath(next) + (query.size ? `?${query}` : '')
}
function hrefPath(next: Route): string {
  switch (next.name) {
    case 'home':
      return '/'
    case 'workspace':
      return `/workspace/${next.domain ?? 'comic'}${workspaceSuffix(next)}`
    case 'workspace_run':
      return `/workspace/${next.domain ?? 'comic'}/run/${encodeURIComponent(next.runId ?? '')}${workspaceSuffix(next)}`
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

export function navigate(next: Route, replace = false): void {
  const path = href(next)
  if (window.location.pathname + (window.location.search ?? '') !== path) {
    if (replace) window.history.replaceState({}, '', path)
    else window.history.pushState({}, '', path)
  }
  route.value = { ...next }
  rememberWorkspace(next)
  window.scrollTo({ top: 0 })
}

export function openRun(domain: string, runId: string): void {
  navigate({ name: 'workspace_run', domain, runId })
}

window.addEventListener('popstate', () => {
  route.value = parse(window.location.pathname + (window.location.search ?? ''))
  rememberWorkspace(route.value)
})
rememberWorkspace(route.value)
