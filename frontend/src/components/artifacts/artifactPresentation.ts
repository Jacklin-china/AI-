import type { CoreArtifact } from '../../types'
import { artifactName as coreArtifactName, artifactPayload } from '../../corePresentation'
import { presenterFor } from '../../domains/presenters'
import { finalImagePrompt } from '../../domains/comic/shotGeneration'
import { resolveComicPreviewUrl } from '../../domains/comic/workspacePresentation'
import { getArtifactContentUrl, getTaskImageUrl } from '../../services/core'

const schemaLabels: Record<string, string> = {
  'commerce.requirement': '商品需求', 'commerce.candidate_list': '候选商品',
  'commerce.candidate_analysis': '选品分析', 'commerce.sku_selection': 'SKU 选择',
  'commerce.pricing_result': '定价结果', 'commerce.listing_draft': 'Listing 草稿',
  'commerce.localized_listing': '俄语 Listing', 'commerce.product_image': '商品主图',
  'commerce.qc_report': 'QC 报告', 'commerce.marketplace_draft': 'Marketplace 草稿',
  'commerce.publish_result': '发布结果',
}

export function assetDisplayName(artifact: CoreArtifact, domain: string): string {
  const schema = String(presentableArtifact(artifact).metadata.schema_name ?? '')
  if (schemaLabels[schema]) return schemaLabels[schema]
  if (artifact.source.startsWith('commerce.')) return coreArtifactName(artifact)
  const name = presenterFor(domain).artifactName(artifact.source, artifact.type)
  if (name !== artifact.source.replace(/[._]/g, ' ')) return name
  return ({ image: '图片', video: '视频', prompt: '提示词', document: '文档', report: '报告', listing: '商品文案', json: '结构化内容' } as Record<string, string>)[artifact.type] ?? '资产'
}

export function artifactOrigin(artifact: CoreArtifact): string {
  const origin = artifact.metadata.origin
  if (typeof origin === 'string' && origin.trim()) return origin
  return artifact.metadata.mock === true ? 'mock' : 'unknown'
}

const legacySchemas: Record<string, string> = {
  'commerce.requirement': 'commerce.requirement',
  'commerce.source.mock': 'commerce.candidate_list',
  'commerce.analysis': 'commerce.candidate_analysis',
  'commerce.sku': 'commerce.sku_selection',
  'commerce.listing': 'commerce.listing_draft',
  'commerce.localization': 'commerce.localized_listing',
  'commerce.qc.mock': 'commerce.qc_report',
  'commerce.marketplace.mock': 'commerce.marketplace_draft',
  'commerce.publish.mock': 'commerce.publish_result',
}

/** Read-only presentation of legacy records; never rewrite persisted provenance. */
export function presentableArtifact(artifact: CoreArtifact): CoreArtifact {
  if (artifact.metadata.schema_name) return artifact
  const schema = legacySchemas[artifact.source]
  if (!schema) return artifact
  let payload = artifactPayload(artifact)
  if (artifact.source === 'commerce.source.mock' && Array.isArray(artifact.metadata.payload)) {
    payload = { items: artifact.metadata.payload }
  } else if (artifact.source === 'commerce.requirement') {
    payload = { ...payload, query: payload.query ?? payload.requirement }
  } else if (artifact.source === 'commerce.localization' && payload.listing && typeof payload.listing === 'object') {
    payload = { ...payload.listing as Record<string, unknown>, locale: payload.locale }
  }
  return { ...artifact, metadata: { ...artifact.metadata, schema_name: schema, payload } }
}

const labels: Record<string, string> = {
  title: '标题', name: '名称', content: '正文', text: '正文', body: '正文', description: '说明',
  requirement: '需求', query: '需求', locale: '语言', message: '说明', summary: '摘要',
  items: '候选商品', id: '编号', source: '来源', listing: '商品信息', sku: 'SKU', skus: 'SKU',
  recommended_id: '推荐候选', candidate_count: '候选数量', basis: '分析依据', candidate_id: '候选编号',
  cost_fen: '采购成本', price_fen: '售价', margin_rate: '毛利率', currency: '币种', revision: '修订',
  status: '状态', passed: '检查通过', checks: '检查项', rework_count: '返工次数',
  marketplace: '销售平台', draft_id: '草稿编号', model: '模型', director_summary: '导演说明',
  positive_prompt: '正向提示词', negative_prompt: '负向约束', final_prompt: '完整提示词',
}
const technicalFields = /^(schema_name|schema_version|origin|mock|domain|trace_id|request_id|project_id|shot_id|storyboard_id|prompt_id|sha256|model_target|compiler_version|context_version|context_sources|character_asset_versions|scene_asset_versions|style_version|creative_brief_version|director_spec_version|storyboard_version|shot_version|parent_artifact_id|asset_artifact_id|private_reasoning|reasoning)$/
export interface ContentRow { label: string; value: string }

/** Flatten readable payload values, including nested arrays, without a JSON dump. */
export function artifactRows(artifact: CoreArtifact): ContentRow[] {
  const rows: ContentRow[] = []
  const visit = (value: unknown, path: string, key = ''): void => {
    if (value && typeof value === 'object') {
      const entries = Object.entries(value)
      if (!entries.length) rows.push({ label: path, value: '无' })
      for (const [child, item] of entries) {
        if (technicalFields.test(child)) continue
        const label = Array.isArray(value) ? String(Number(child) + 1) : labels[child] ?? child.replaceAll('_', ' ')
        visit(item, path ? `${path} / ${label}` : label, child)
      }
    } else {
      let text = value == null ? '未提供' : typeof value === 'boolean' ? (value ? '是' : '否') : String(value)
      if (typeof value === 'number' && key.endsWith('_fen')) text = `¥${(value / 100).toFixed(2)}`
      if (typeof value === 'number' && key === 'margin_rate') text = `${Math.round(value * 100)}%`
      rows.push({ label: path, value: text })
    }
  }
  const content = artifact.metadata.payload ?? artifact.metadata
  visit(content, content && typeof content === 'object' ? '' : '正文')
  return rows.filter(row => row.label)
}

export function artifactText(artifact: CoreArtifact): string {
  if (typeof artifact.metadata.payload === 'string') return artifact.metadata.payload
  const payload = artifactPayload(artifact)
  if (artifact.type === 'prompt') {
    if (artifact.source === 'comic.prompt.compiler') return finalImagePrompt(payload)
    return String(payload.final_prompt ?? payload.prompt ?? payload.content ?? payload.text ?? finalImagePrompt(payload))
  }
  return artifactRows(artifact).map(row => `${row.label}：${row.value}`).join('\n\n')
}

export function artifactFilename(artifact: CoreArtifact, name: string): string {
  const filename = String(artifact.metadata.filename ?? '')
  const extension = (filename || artifact.location || '').match(/\.([a-z0-9]+)$/i)?.[1]?.toLowerCase()
  const allowed = artifact.type === 'image' ? ['png', 'jpg', 'jpeg', 'webp', 'gif'] : ['mp4', 'webm', 'mov']
  const suffix = allowed.includes(extension ?? '') ? extension : artifact.type === 'image' ? 'png' : 'mp4'
  return `${name.replace(/[<>:"/\\|?*\x00-\x1f]/g, '-')}-v${artifact.version}.${suffix}`
}

export async function loadArtifactMedia(artifact: CoreArtifact): Promise<string> {
  let failure: unknown
  const capture = async (loader: () => Promise<string | null>): Promise<string | null> => {
    try { return await loader() } catch (error) { failure = error; return null }
  }
  const url = artifact.source === 'comic.archive'
    ? await resolveComicPreviewUrl(artifact, String(artifact.metadata.request_id ?? '') || null,
      id => capture(() => getArtifactContentUrl(id, true)),
      id => capture(() => getTaskImageUrl(id, true)))
    : await capture(() => getArtifactContentUrl(artifact.id, true))
  if (!url) throw failure ?? new Error('媒体文件不可用，请检查原文件是否仍存在。')
  return url
}
