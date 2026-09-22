<script setup lang="ts">
import { computed } from 'vue'
import type { CoreArtifact } from '../../../types'
const props = defineProps<{ artifact: CoreArtifact }>()
const payload = computed(() => props.artifact.metadata.payload as Record<string, unknown>)
const money = (fen: unknown): string => `¥${(Number(fen ?? 0) / 100).toFixed(2)}`
</script>
<template>
  <div class="listing-preview">
    <div><span>{{ String(payload.locale ?? 'zh-CN') }}</span><strong>{{ money(payload.price_fen) }}</strong></div>
    <h4>{{ String(payload.title ?? '未命名 Listing') }}</h4>
    <p>{{ String(payload.description ?? '') }}</p>
    <small>SKU {{ String(payload.sku ?? '—') }} · 第 {{ Number(payload.revision ?? 0) + 1 }} 版</small>
  </div>
</template>
