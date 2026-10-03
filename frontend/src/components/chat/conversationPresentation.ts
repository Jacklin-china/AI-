/** Navigation labels only: never alter the creative request sent to the agent. */
export function conversationTaskTitle(request: string): string {
  const text = request.trim().replace(/\s+/g, ' ')
    .replace(/^(?:我想|我要|我希望|请|麻烦|帮我|给我|能否|可以|你能|再|继续|来|做|制作|生成|画|一张|一个|一幅|一段|一部)+/u, '')
    .replace(/^(?:中式|东方|山海经)/u, '')
  const scene = text.match(/^(.+?)(?:站在|坐在|躺在|走在|在)(.+?)(?:边|上|里|中)?(?:看|望|眺望|欣赏|打|等待|思念|$)/u)
  const label = scene ? `${scene[1]}${scene[2]}场景` : text.replace(/[，。！？：；].*$/u, '')
  return Array.from(label || text || '新对话').slice(0, 14).join('')
}

export function ownsConversationRun(run: { state: Record<string, unknown> }, id: string): boolean {
  return !!id && run.state.conversation_id === id
}
