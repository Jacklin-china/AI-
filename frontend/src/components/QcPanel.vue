<script setup lang="ts">
import type { QcResult } from '../types'
import StatusBadge from './StatusBadge.vue'

const props = defineProps<{ qc: QcResult | null }>()
const resultStatus = (): string => {
  if (!props.qc) return 'pending'
  return props.qc.broken_hands || props.qc.watermark || !props.qc.composition_ok ? 'failed' : 'completed'
}
</script>

<template>
  <section class="subpanel qc-panel">
    <header><div><span class="section-kicker">质检</span><h3>质量检查</h3></div><StatusBadge :status="resultStatus()" :label="qc ? (resultStatus() === 'completed' ? '通过' : '注意') : '待检'" /></header>
    <div v-if="qc" class="qc-content"><div class="qc-score"><strong>{{ Math.round(qc.confidence * 100) }}</strong><span>Confidence</span></div><div><b>Evaluator · VLM</b><p>{{ qc.reason }}</p><ul><li>构图 {{ qc.composition_ok ? '通过' : '需复核' }}</li><li>人物一致性 {{ qc.persona_consistency }}/5</li><li v-if="qc.broken_hands">检测到手部风险</li><li v-if="qc.watermark">检测到水印风险</li></ul></div></div>
    <div v-else class="panel-placeholder">尚未运行视觉预筛。结果只作为人工终审建议。</div>
  </section>
</template>
