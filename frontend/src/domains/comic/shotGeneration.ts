import type { CoreRun } from '../../types'
import type { ComicImageView } from '../../services/core'

/** Project + Shot + immutable request identity; navigation never owns image state. */
export function shotGeneration(runs: CoreRun[], projectId: string, shotId: string): CoreRun | null {
  return runs.filter(run => {
    const creation = run.state.quick_creation as Record<string, unknown> | undefined
    return run.workflow === 'comic.production.v1' && creation?.project_id === projectId && creation?.shot_id === shotId
  }).sort((a, b) => b.started_at.localeCompare(a.started_at))[0] ?? null
}

/** Media preparation is derived from immutable Prompt provenance, not Shot.status. */
export function externalShotPrompt(prompts: Record<string, unknown>[], shotVersion: number, directorVersion: number,
  source: { projectId: string; shotId: string; storyboardId: string; storyboardVersion: number; briefVersion: number }): Record<string, unknown> | null {
  return prompts.find(prompt => prompt.model_target === 'external' && !!prompt.artifact_id
    && prompt.shot_version === shotVersion && prompt.director_spec_version === directorVersion
    && prompt.project_id === source.projectId && prompt.shot_id === source.shotId
    && prompt.storyboard_id === source.storyboardId && prompt.storyboard_version === source.storyboardVersion
    && prompt.creative_brief_version === source.briefVersion && prompt.context_version === 'image-bibles-1'
    && ['character_context', 'world_context', 'style_context', 'shot_context']
      .every(field => typeof prompt[field] === 'string')
    && ['character_context', 'world_context', 'style_context', 'shot_context']
      .some(field => String(prompt[field]).trim())
    && ['positive_prompt', 'negative_prompt']
      .every(field => typeof prompt[field] === 'string' && String(prompt[field]).trim())) ?? null
}

/** Clipboard/export is the saved complete Image Prompt, including negative constraints. */
export function finalImagePrompt(prompt: Record<string, unknown>): string {
  if (typeof prompt.final_prompt === 'string' && prompt.final_prompt.trim()) return prompt.final_prompt
  const negative = String(prompt.negative_prompt ?? '').trim()
  return String(prompt.positive_prompt ?? '') + (negative ? `\n\nNegative Constraint / 禁止内容\n${negative}` : '')
}

export interface PromptReference {
  artifact_id: string; filename: string; sha256: string; version: number
}

/** Keep required attachments visible even if an older record has incomplete metadata. */
export function imagePromptReferences(prompt: Record<string, unknown>): PromptReference[] {
  if (!Array.isArray(prompt.reference_attachments)) return []
  return prompt.reference_attachments.map(value => {
    const item = value && typeof value === 'object' ? value as Record<string, unknown> : {}
    const image = item.image && typeof item.image === 'object' ? item.image as Record<string, unknown> : {}
    return { artifact_id: typeof item.artifact_id === 'string' ? item.artifact_id : '',
      filename: typeof item.filename === 'string' ? item.filename : '参考图片',
      sha256: typeof item.sha256 === 'string' ? item.sha256 : '', version: Number(image.version ?? 1) }
  }).filter((item, index, all) => all.findIndex(other => other.artifact_id === item.artifact_id
    && other.sha256 === item.sha256) === index)
}

/** Read the exact saved bytes through the authenticated Artifact gateway, never a public URL. */
export async function readPromptReference(reference: PromptReference,
  load: (id: string) => Promise<Blob | null>): Promise<Blob> {
  if (!reference.artifact_id || !/^[a-f\d]{64}$/i.test(reference.sha256)) throw new Error('参考图来源不完整，请重新读取当前指令')
  const content = await load(reference.artifact_id)
  if (!content) throw new Error('参考图暂时无法读取，请重试或从回收站恢复原图')
  const bytes = await content.arrayBuffer()
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)))
    .map(value => value.toString(16).padStart(2, '0')).join('')
  if (digest !== reference.sha256.toLowerCase()) throw new Error('参考图内容与指令记录不一致，已停止复制')
  return content
}

async function clipboardPng(content: Blob): Promise<Blob> {
  if (content.type === 'image/png') return content
  const image = await createImageBitmap(content)
  try {
    const canvas = document.createElement('canvas')
    canvas.width = image.width; canvas.height = image.height
    const context = canvas.getContext('2d')
    if (!context) throw new Error('浏览器无法准备参考图，请使用下载原图')
    context.drawImage(image, 0, 0)
    return await new Promise<Blob>((resolve, reject) => canvas.toBlob(
      blob => blob ? resolve(blob) : reject(new Error('无法复制参考图，请使用下载原图')), 'image/png'))
  } finally { image.close() }
}

/** Clipboard formats are alternatives; the receiving app decides which ones it pastes. */
export async function copyPromptDelivery(prompt: Record<string, unknown>,
  load: (id: string) => Promise<Blob | null>, mode: 'combined' | 'text' | 'image' = 'combined',
  reference?: PromptReference): Promise<'combined' | 'text' | 'image'> {
  const text = finalImagePrompt(prompt)
  const references = imagePromptReferences(prompt)
  if (mode === 'text' || (!references.length && mode !== 'image')) {
    await navigator.clipboard.writeText(text)
    return 'text'
  }
  if (!navigator.clipboard?.write || typeof ClipboardItem === 'undefined') throw new Error('浏览器不支持复制图片，请使用下载原图；文字可单独复制')
  if (mode !== 'image' && references.length !== 1) throw new Error('这份指令需要多张参考图，请在下方逐张复制或下载原图，再单独复制文字')
  const chosen = reference ?? references[0]
  if (!chosen || !references.some(item => item.artifact_id === chosen.artifact_id && item.sha256 === chosen.sha256)) throw new Error('参考图不属于这份指令')
  const png = readPromptReference(chosen, load).then(clipboardPng)
  const data: Record<string, Blob | Promise<Blob>> = { 'image/png': png }
  if (mode === 'combined') {
    data['text/plain'] = new Blob([text], { type: 'text/plain' })
    data['text/html'] = png.then(content => new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = () => reject(new Error('无法准备图文，请分别复制图片和文字'))
      reader.readAsDataURL(content)
    })).then(url => {
      const escape = (value: string) => value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
      return new Blob([`<img src="${url}" alt="${escape(chosen.filename)}"><pre>${escape(text)}</pre>`], { type: 'text/html' })
    })
  }
  // Start the native write within the click gesture; image loading stays in Blob promises.
  // Permission rejection can happen before the browser consumes those promises.
  void Promise.allSettled(Object.values(data))
  await navigator.clipboard.write([new ClipboardItem(data)])
  return mode
}

export function promptVersionDiff(before: string, after: string): { before: string; after: string }[] {
  const left = before.split('\n'), right = after.split('\n')
  return Array.from({ length: Math.max(left.length, right.length) }, (_, index) => ({
    before: left[index] ?? '', after: right[index] ?? '',
  })).filter(line => line.before !== line.after)
}

/** Old Shot uploads retain their Core Artifact as v1; direct imports use the media API identity. */
export function reviewImageSources(prompts: Record<string, unknown>[], images: ComicImageView[], conversationId: string): ComicImageView[] {
  const sources = images.filter(image => image.conversation_id === conversationId)
  for (const prompt of prompts) {
    const external = prompt.external_image as Record<string, unknown> | null
    if (!external?.artifact_id || sources.some(image => image.artifact_id === external.artifact_id)) continue
    sources.push({ image_id: String(external.artifact_id), version: 1, artifact_id: String(external.artifact_id),
      project_id: String(prompt.project_id), conversation_id: conversationId,
      filename: String(external.filename ?? 'external-image.png'), shot_id: String(prompt.shot_id),
      label: `镜头 ${prompt.shot_sequence_number ?? '?'} · ${external.filename ?? '成图'}` })
  }
  return sources
}
