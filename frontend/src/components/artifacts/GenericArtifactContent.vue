<script setup lang="ts">
import { computed } from 'vue'
import type { CoreArtifact } from '../../types'

const props = defineProps<{ artifact: CoreArtifact }>()
const payload = computed(() => (props.artifact.metadata.payload ?? {}) as Record<string, unknown>)
const rows = computed(() => Object.entries(payload.value).slice(0, 5))
</script>

<template>
  <dl v-if="rows.length" class="artifact-facts">
    <div v-for="([key, value]) in rows" :key="key">
      <dt>{{ key.replaceAll('_', ' ') }}</dt>
      <dd>{{ typeof value === 'object' ? '结构化内容' : String(value) }}</dd>
    </div>
  </dl>
  <p v-else class="artifact-empty-note">该产物没有可展示的摘要，可在详情中查看技术数据。</p>
</template>
