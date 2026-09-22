import type { RuntimeEvent } from '../services/core'

/* Domain UI 层：只有这里理解领域名词，Core 事件保持通用。 */

export interface DomainPresenter {
  id: string
  label: string
  subtitle: string
  workflow: { id: string; label: string }[]
  guideHint: string
  examples: string[]
  nodeLabel: (nodeId: string) => string
  eventText: (event: RuntimeEvent) => string | null
  artifactName: (source: string, type: string) => string
  approvalTitle: (kind: string | null) => string
}

const fallbackName = (source: string): string => source.replace(/[._]/g, ' ')

const commerceLabels: Record<string, string> = {
  requirement: '需求理解',
  source_search: '采集候选商品',
  candidate_analysis: '候选分析',
  candidate_approval: '商品确认',
  sku_selection: 'SKU 选择',
  pricing: '定价',
  listing_draft: '生成 Listing',
  localize: '俄语本地化',
  asset_generation: '生成商品主图',
  qc: '质量检查',
  rework: '返工',
  publish_approval: '发布确认',
  create_marketplace_draft: '创建平台草稿',
  publish_draft: '发布草稿',
}

const comicLabels: Record<string, string> = {
  prepare: '需求与分镜准备',
  cost_approval: '费用确认',
  generate: '图片生成',
  video: '图生视频',
  qc: '质量检查',
  human_review: '人工审核',
  rework: '返工',
  archive: '归档交付',
}

const studioLabels: Record<string, string> = {
  prepare: '需求理解',
  cost_approval: '费用确认',
  generate: '生成图片',
  qc: '质量检查',
  human_review: '人工审核',
  archive: '归档交付',
}

function nodeList(labels: Record<string, string>, ids: string[]): { id: string; label: string }[] {
  return ids.map((id) => ({ id, label: labels[id] ?? id }))
}

const commerceFlow = ['requirement', 'source_search', 'candidate_analysis', 'candidate_approval', 'sku_selection', 'pricing', 'listing_draft', 'localize', 'asset_generation', 'qc', 'publish_approval', 'create_marketplace_draft', 'publish_draft']
const comicFlow = ['prepare', 'cost_approval', 'generate', 'qc', 'human_review', 'archive', 'video']
const studioFlow = ['prepare', 'cost_approval', 'generate', 'qc', 'human_review', 'archive']

const commerceArtifacts: Record<string, string> = {
  'commerce.requirement': '需求说明',
  'commerce.source.mock': '候选商品清单',
  'commerce.analysis': '候选分析报告',
  'commerce.sku': 'SKU 选择结果',
  'commerce.pricing': '定价结果',
  'commerce.listing': 'Listing 草稿',
  'commerce.localization': '俄语 Listing',
  'commerce.image.mock': '商品主图',
  'commerce.image.real': '商品主图',
  'commerce.qc.mock': 'QC 报告',
  'commerce.rework': '返工记录',
  'commerce.marketplace.mock': '平台草稿',
  'commerce.publish.mock': '发布结果',
}

function genericEventText(
  event: RuntimeEvent,
  label: (nodeId: string) => string,
): string | null {
  const node = event.node_id ?? ''
  switch (event.event_type) {
    case 'run_started':
      return '任务已创建，开始执行。'
    case 'node_started':
      return `正在${label(node)}……`
    case 'node_retrying':
      return `${label(node)}未成功，正在重试（第 ${String(event.payload.retry_count ?? 1)} 次）。`
    case 'node_failed':
      return `${label(node)}失败：${String(event.payload.safe_message ?? '请稍后重试')}`
    case 'run_waiting':
      return '这一步需要你确认。'
    case 'approval_resolved':
      return ({
        approve: '已确认，继续执行。',
        reject: '已拒绝，流程按拒绝路由结束。',
        request_revision: '已收到修改要求，正在重新处理……',
      } as Record<string, string>)[String(event.payload.decision ?? '')] ?? '审批已处理。'
    case 'run_completed':
      return '任务完成。'
    case 'run_failed':
      return `任务失败：${String(event.payload.safe_message ?? '请稍后重试')}`
    case 'cost_updated':
      return `费用已更新：¥${(Number(event.payload.cost_fen ?? 0) / 100).toFixed(2)}`
    case 'run_cancelled':
      return '任务已取消。'
    default:
      return null
  }
}

const commerce: DomainPresenter = {
  id: 'commerce',
  label: '电商',
  subtitle: '选品、定价、本地化、商品图、QC 与发布工作流。',
  workflow: nodeList(commerceLabels, commerceFlow),
  guideHint: '描述你要做的商品或选品方向，我会先和你逐项确认需求，再逐步推进采集、定价与发布。',
  examples: [
    '帮我在 Ozon 上找 3 款有潜力的宠物保温杯，给出定价建议',
    '把这款蓝牙耳机的 Listing 翻译成俄语并本地化',
    '为这款厨房收纳盒生成 4 张白底主图并质检',
  ],
  nodeLabel: (nodeId) => commerceLabels[nodeId] ?? nodeId,
  eventText: (event) => genericEventText(event, (node) => commerceLabels[node] ?? node),
  artifactName: (source) => commerceArtifacts[source] ?? fallbackName(source),
  approvalTitle: (kind) =>
    kind === 'candidate_approval' ? '需要你确认候选商品' : '发布前确认',
}

const comic: DomainPresenter = {
  id: 'comic',
  label: '漫剧',
  subtitle: '分镜、角色一致性、生图、QC、人工审核与归档。',
  workflow: nodeList(comicLabels, comicFlow),
  guideHint: '从剧本或分镜想法开始，我会先和你确认风格与镜头要求，确认无误后回复「开始」提交执行。',
  examples: [
    '雨夜便利店，主角捡到一只会说话的猫，生成开场分镜',
    '来一张赛博朋克风的女主角立绘，霓虹光，侧脸特写',
    '生成一张古风仙侠对决场景图，水墨质感',
  ],
  nodeLabel: (nodeId) => comicLabels[nodeId] ?? nodeId,
  eventText: (event) => genericEventText(event, (node) => comicLabels[node] ?? node),
  artifactName: (source) =>
    ({ 'comic.archive': '成片归档', 'comic.image': '分镜图' } as Record<string, string>)[source] ??
    fallbackName(source),
  approvalTitle: () => '需要你审核当前画面',
}

const studio: DomainPresenter = {
  id: 'studio',
  label: '通用创作',
  subtitle: '自由描述需求，生成图片与质量检查。',
  workflow: nodeList(studioLabels, studioFlow),
  guideHint: '告诉我画面主题、风格、比例和数量，我会逐步和你确认；确认无误后回复「开始」提交生成。',
  examples: [
    '生成一张写实风的猫咪肖像，暖光，浅景深',
    '做一张极简风的产品海报：白色背景，一只保温杯',
    '画一张动漫风的少年侧脸，黄昏光线，电影感',
  ],
  nodeLabel: (nodeId) => studioLabels[nodeId] ?? comicLabels[nodeId] ?? nodeId,
  eventText: (event) => genericEventText(event, (node) => studioLabels[node] ?? comicLabels[node] ?? node),
  artifactName: (source) =>
    ({ 'comic.archive': '成片归档', 'comic.image': '生成图片' } as Record<string, string>)[source] ??
    fallbackName(source),
  approvalTitle: (kind) => (kind === 'cost_approval' ? '生成前费用确认' : '需要你审核当前画面'),
}

const registry: Record<string, DomainPresenter> = { commerce, comic, studio }

export function presenterFor(domainId: string): DomainPresenter {
  return registry[domainId] ?? studio
}

export const domainOrder = ['commerce', 'comic', 'studio']
