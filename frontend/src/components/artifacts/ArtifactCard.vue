<script setup lang="ts">
import { computed, ref } from 'vue'
import { FileText, Film, Image as ImageIcon, ListTree } from 'lucide-vue-next'
import type { CoreArtifact } from '../../types'
import { artifactName as coreArtifactName } from '../../corePresentation'
import { presenterFor } from '../../domains/presenters'
import { resolveArtifactRenderer } from './rendererRegistry'

const props = defineProps<{ artifact: CoreArtifact; domain: string }>()
const expanded = ref(false)
const presenter = computed(() => presenterFor(props.domain))
const schemaName = computed(() => String(props.artifact.metadata.schema_name ?? 'generic'))
const schemaLabels: Record<string, string> = {
  'commerce.requirement': '商品需求',
  'commerce.candidate_list': '候选商品',
  'commerce.candidate_analysis': '选品分析',
  'commerce.sku_selection': 'SKU 选择',
  'commerce.pricing_result': '定价结果',
  'commerce.listing_draft': 'Listing 草稿',
  'commerce.localized_listing': '俄语 Listing',
  'commerce.product_image': '商品主图',
  'commerce.qc_report': 'QC 报告',
  'commerce.marketplace_draft': 'Marketplace 草稿',
  'commerce.publish_result': '发布结果',
}
const name = computed(() => {
  const schemaLabel = schemaLabels[schemaName.value]
  if (schemaLabel) return schemaLabel
  if (props.artifact.source.startsWith('commerce.')) return coreArtifactName(props.artifact)
  return presenter.value.artifactName(props.artifact.source, props.artifact.type)
})
const renderer = computed(() => resolveArtifactRenderer(schemaName.value))
const origin = computed(() => String(
  props.artifact.metadata.origin ?? (props.artifact.metadata.mock ? 'mock' : 'real'),
))
const originLabel = computed(() => ({ real: 'Real', mock: 'Mock', blocked: 'Blocked' }[origin.value] ?? origin.value))
const icon = computed(() =>
  ({ image: ImageIcon, video: Film, listing: ListTree } as Record<string, typeof FileText>)[props.artifact.type] ?? FileText,
)
</script>

<template>
  <article class="artifact-card" :data-type="artifact.type">
    <header>
      <i><component :is="icon" :size="14" :stroke-width="1.8" /></i>
      <div><strong>{{ name }}</strong><small>{{ new Date(artifact.created_at).toLocaleString() }}</small></div>
      <span class="origin-label" :data-origin="origin">{{ originLabel }}</span>
    </header>
    <component :is="renderer" :artifact="artifact" />
    <footer>
      <span>版本 {{ artifact.version }}</span>
      <button class="text-action" @click="expanded = !expanded">{{ expanded ? '收起技术详情' : '查看详情' }}</button>
    </footer>
    <div v-if="expanded" class="artifact-technical">
      <dl><div><dt>Artifact ID</dt><dd>{{ artifact.id }}</dd></div><div><dt>Source</dt><dd>{{ artifact.source }}</dd></div><div><dt>Schema</dt><dd>{{ schemaName }}</dd></div></dl>
      <pre class="raw-json">{{ JSON.stringify(artifact.metadata, null, 2) }}</pre>
    </div>
  </article>
</template>
