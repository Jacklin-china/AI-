<script setup lang="ts">
import { computed } from 'vue'
import MediaArtifactContent from '../../../components/artifacts/MediaArtifactContent.vue'
import type { CoreArtifact } from '../../../types'
const props = defineProps<{ artifact: CoreArtifact; detailed?: boolean }>()
const payload = computed(() => props.artifact.metadata.payload as Record<string, unknown>)
</script>
<template>
  <MediaArtifactContent :artifact="artifact" :detailed="detailed" />
  <dl class="artifact-facts image-brief-facts">
    <div><dt>用途</dt><dd>Marketplace 商品主图</dd></div>
    <div><dt>模型</dt><dd>{{ String(payload.model ?? '—') }}</dd></div>
  </dl>
</template>
