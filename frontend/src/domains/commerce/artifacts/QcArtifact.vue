<script setup lang="ts">
import { computed } from 'vue'
import type { CoreArtifact } from '../../../types'
const props = defineProps<{ artifact: CoreArtifact }>()
const payload = computed(() => props.artifact.metadata.payload as Record<string, unknown>)
const checks = computed(() => (payload.value.checks ?? []) as string[])
</script>
<template>
  <div class="qc-summary" :data-passed="Boolean(payload.passed)">
    <strong>{{ payload.passed ? 'QC 通过' : 'QC 未通过' }}</strong>
    <span>第 {{ Number(payload.rework_count ?? 0) + 1 }} 次检查</span>
    <ul><li v-for="item in checks" :key="item">{{ item.replaceAll('_', ' ') }}</li></ul>
  </div>
</template>
