import type { CoreArtifact, CoreRun } from './types'

export type UnknownRecord = Record<string, unknown>

const artifactNames: Record<string, string> = {
  'commerce.publish.mock': '发布结果',
  'commerce.marketplace.mock': 'Marketplace 草稿',
  'commerce.qc.mock': 'QC 报告',
  'commerce.localization': '俄语 Listing',
  'commerce.image.mock': '商品主图',
  'commerce.pricing': '定价结果',
  'commerce.sku': 'SKU 选择',
  'commerce.analysis': '选品分析',
  'commerce.source.mock': '候选商品',
  'commerce.listing': 'Listing 草稿',
  'commerce.requirement': '商品需求',
  'commerce.rework': '返工记录',
}

const nodeNames: Record<string, string> = {
  __end__: '已结束',
  requirement: '解析需求',
  source_search: '查找候选商品',
  candidate_analysis: '分析候选商品',
  candidate_approval: '候选商品审批',
  sku_selection: '选择 SKU',
  pricing: '计算定价',
  listing_draft: '编写 Listing',
  localize: '本地化 Listing',
  asset_generation: '生成商品素材',
  qc: '质量检查',
  rework: '素材返工',
  publish_approval: '发布前审批',
  create_marketplace_draft: '创建 Marketplace 草稿',
  publish_draft: '提交发布草稿',
}

export function asRecord(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : {}
}

export function asRecords(value: unknown): UnknownRecord[] {
  return Array.isArray(value) ? value.map(asRecord) : []
}

export function text(value: unknown, fallback = '未提供'): string {
  return typeof value === 'string' && value.trim() ? value : fallback
}

export function formatMoney(fen: unknown): string {
  return typeof fen === 'number' && Number.isFinite(fen) ? `¥${(fen / 100).toFixed(2)}` : '未提供'
}

export function formatTime(raw: string): string {
  const date = new Date(raw)
  return Number.isNaN(date.valueOf())
    ? raw
    : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function nodeName(id: string): string {
  return nodeNames[id] ?? id.replaceAll('_', ' ')
}

export function runName(run: CoreRun): string {
  const listing = asRecord(run.state.localized_listing)
  return text(run.state.requirement, text(listing.title, `${run.domain} Production`))
}

export function artifactPayload(artifact: CoreArtifact): UnknownRecord {
  const payload = artifact.metadata.payload
  return payload !== undefined ? asRecord(payload) : artifact.metadata
}

export function artifactName(artifact: CoreArtifact): string {
  if (artifact.source === 'commerce.localization') {
    const locale = text(artifactPayload(artifact).locale, '')
    return locale === 'ru-RU' ? '俄语 Listing' : '本地化 Listing'
  }
  return artifactNames[artifact.source] ?? ({
    image: '图片素材', video: '视频素材', listing: 'Listing', report: '报告',
    prompt: 'Prompt', document: '文档', json: '数据记录',
  }[artifact.type] ?? 'Artifact')
}

export function artifactSummary(artifact: CoreArtifact): string {
  const payload = artifactPayload(artifact)
  if (artifact.source === 'commerce.publish.mock') return `Marketplace 状态：${text(payload.status)}`
  if (artifact.source === 'commerce.marketplace.mock') return text(asRecord(payload.listing).title, 'Marketplace 发布草稿')
  if (artifact.source === 'commerce.qc.mock') return `${payload.passed === true ? '检查通过' : '检查未通过'}，返工 ${Number(payload.rework_count ?? 0)} 次`
  if (artifact.source === 'commerce.localization') return text(asRecord(payload.listing).title, '本地化商品信息')
  if (artifact.source === 'commerce.image.mock') return `商品主图版本 ${artifact.version}`
  if (artifact.source === 'commerce.pricing') return `建议售价 ${formatMoney(payload.price_fen)}`
  if (artifact.source === 'commerce.sku') return `已选择 ${text(payload.sku)}`
  if (artifact.source === 'commerce.analysis') return `已分析 ${Number(payload.candidate_count ?? 0)} 个候选商品`
  if (artifact.source === 'commerce.source.mock') return `共 ${Array.isArray(artifact.metadata.payload) ? artifact.metadata.payload.length : 0} 个候选商品`
  if (artifact.source === 'commerce.requirement') return text(payload.requirement)
  if (artifact.source === 'commerce.listing') return text(payload.title, '商品 Listing 草稿')
  return text(payload.title, text(payload.name, artifact.location ?? '已持久化产物'))
}

export function artifactIsMock(artifact: CoreArtifact): boolean {
  const payload = artifactPayload(artifact)
  return artifact.metadata.mock === true || payload.mock === true || artifact.source.includes('.mock')
}
