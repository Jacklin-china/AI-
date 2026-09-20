export type DomainStatus = 'available' | 'preview' | 'coming_soon'
export type SkillStatus = 'available' | 'preview'

export interface DomainSkill {
  id: string
  name: string
  description: string
  status: SkillStatus
  requiredTools: string[]
}

export interface DomainDefinition {
  id: 'comic' | 'commerce' | 'ads' | 'studio'
  name: string
  label: string
  icon: string
  description: string
  status: DomainStatus
  capabilities: string[]
  workflows: string[]
  skills: DomainSkill[]
}

export const domains: DomainDefinition[] = [
  {
    id: 'comic',
    name: 'Comic',
    label: '漫剧',
    icon: 'CM',
    description: '分镜、角色约束与画面生产',
    status: 'preview',
    capabilities: ['Storyboard', 'Character Context', 'Image Generation'],
    workflows: ['Storyboard to Image'],
    skills: [
      { id: 'storyboard', name: 'Storyboard Generation', description: '将剧本拆解为结构化分镜。', status: 'available', requiredTools: ['LLM', 'Storyboard Tool'] },
      { id: 'character-consistency', name: 'Character Consistency', description: '按镜头裁剪角色、场景与连续性约束。', status: 'available', requiredTools: ['Persona Memory', 'Prompt Factory'] },
    ],
  },
  {
    id: 'commerce',
    name: 'Commerce',
    label: '电商',
    icon: 'CO',
    description: '跨境商品研究、定价、本地化、素材质检与发布审批；外部平台当前使用显式 Mock Adapter。',
    status: 'available',
    capabilities: ['Product Research', 'Pricing', 'Localization', 'Product Assets', 'Marketplace Draft'],
    workflows: ['Commerce Production v1'],
    skills: [
      { id: 'listing-draft', name: 'Listing Draft', description: '生成并本地化平台 Listing 草稿；Marketplace Adapter 当前为 Mock。', status: 'available', requiredTools: ['Mock Marketplace Adapter'] },
    ],
  },
  {
    id: 'ads',
    name: 'Ads',
    label: '广告',
    icon: 'AD',
    description: '广告内容与营销素材生产',
    status: 'coming_soon',
    capabilities: [],
    workflows: [],
    skills: [],
  },
  {
    id: 'studio',
    name: 'Studio',
    label: '通用创作',
    icon: 'ST',
    description: '当前可运行的通用图片生产工作台',
    status: 'available',
    capabilities: ['Prompt', 'Image Generation', 'QC', 'Human Review', 'Archive'],
    workflows: ['Image Production'],
    skills: [
      { id: 'prompt-design', name: 'Prompt Design', description: '整理主体、构图、光线、材质与受众。', status: 'available', requiredTools: ['LLM'] },
      { id: 'image-production', name: 'Image Production', description: '预算保护下生成单张图片并保留任务记录。', status: 'available', requiredTools: ['Image Provider', 'Budget'] },
      { id: 'quality-review', name: 'Quality Review', description: '视觉预筛、人工终审与定向返修。', status: 'available', requiredTools: ['Vision', 'Archive'] },
    ],
  },
]

export function domainById(id: string): DomainDefinition {
  return domains.find((domain) => domain.id === id) ?? domains[3]
}
