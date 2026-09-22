<script setup lang="ts">
import { computed } from 'vue'
import type { CoreArtifact } from '../../../types'
interface Candidate { id: string; title: string; cost_fen: number; skus: string[]; source: string }
const props = defineProps<{ artifact: CoreArtifact }>()
const payload = computed(() => props.artifact.metadata.payload as Record<string, unknown>)
const schema = computed(() => String(props.artifact.metadata.schema_name ?? ''))
const items = computed(() => (payload.value.items ?? []) as Candidate[])
const money = (fen: unknown): string => `¥${(Number(fen ?? 0) / 100).toFixed(2)}`
</script>
<template>
  <div v-if="schema === 'commerce.candidate_list'" class="artifact-mini-table">
    <div v-for="item in items" :key="item.id">
      <strong>{{ item.title }}</strong><span>{{ money(item.cost_fen) }}</span><small>{{ item.skus.length }} SKU</small>
    </div>
  </div>
  <dl v-else class="artifact-facts">
    <div v-if="schema === 'commerce.candidate_analysis'"><dt>推荐候选</dt><dd>{{ String(payload.recommended_id ?? '—') }}</dd></div>
    <div v-if="schema === 'commerce.candidate_analysis'"><dt>分析依据</dt><dd>{{ String(payload.basis ?? '—') }}</dd></div>
    <div v-if="schema === 'commerce.sku_selection'"><dt>已选 SKU</dt><dd>{{ String(payload.sku ?? '—') }}</dd></div>
    <div v-if="schema === 'commerce.pricing_result'"><dt>采购成本</dt><dd>{{ money(payload.cost_fen) }}</dd></div>
    <div v-if="schema === 'commerce.pricing_result'"><dt>建议售价</dt><dd>{{ money(payload.price_fen) }}</dd></div>
    <div v-if="schema === 'commerce.pricing_result'"><dt>目标毛利</dt><dd>{{ Math.round(Number(payload.margin_rate ?? 0) * 100) }}%</dd></div>
  </dl>
</template>
