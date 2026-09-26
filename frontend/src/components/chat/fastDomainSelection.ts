export type FastDomain = 'comic' | 'commerce' | 'studio'

/** A persisted domain belongs to the composer only until its first task is sent. */
export function visibleFastDomain(
  domain: string | null,
  taskId: string | null,
  consumed: boolean,
): FastDomain | null {
  if (taskId || consumed) return null
  return domain === 'comic' || domain === 'commerce' || domain === 'studio' ? domain : null
}
