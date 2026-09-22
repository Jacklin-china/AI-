<script setup lang="ts">
import { computed } from 'vue'
import type { CoreArtifact } from '../../../types'
const props = defineProps<{ artifact: CoreArtifact }>()
const payload = computed(() => props.artifact.metadata.payload as Record<string, unknown>)
</script>
<template>
  <div class="artifact-copy">
    <p>{{ String(payload.query ?? '—') }}</p>
    <small>目标语言 {{ String(payload.locale ?? '—') }}</small>
    <blockquote v-if="payload.revision_instruction">修改要求：{{ String(payload.revision_instruction) }}</blockquote>
  </div>
</template>
