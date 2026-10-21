<script setup lang="ts">
import { computed } from 'vue'
import type { CoreArtifact } from '../../types'
import { artifactRows, artifactText } from './artifactPresentation'

const props = defineProps<{ artifact: CoreArtifact; detailed?: boolean }>()
const text = computed(() => props.artifact.type === 'prompt' ? artifactText(props.artifact) : '')
const rows = computed(() => {
  const content = artifactRows(props.artifact)
  return props.detailed ? content : content.slice(0, 5)
})
</script>

<template>
  <p v-if="text" class="artifact-readable-text" :class="{ 'artifact-content-summary': !detailed }">{{ text }}</p>
  <dl v-else-if="rows.length" class="artifact-facts" :class="{ 'artifact-content-summary': !detailed }">
    <div v-for="(row, index) in rows" :key="index">
      <dt>{{ row.label }}</dt>
      <dd>{{ row.value }}</dd>
    </div>
  </dl>
  <p v-else class="artifact-empty-note">尚无可展示的内容。</p>
</template>

<style scoped>
.artifact-readable-text, dd { white-space: pre-wrap; overflow-wrap: anywhere; }
.artifact-readable-text { line-height: 1.7; font-size: 13px; }
.artifact-content-summary { max-height: 180px; overflow: hidden; }
</style>
