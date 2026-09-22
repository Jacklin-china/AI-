<script setup lang="ts">
import { computed } from 'vue'
import type { CoreArtifact } from '../../../types'
const props = defineProps<{ artifact: CoreArtifact }>()
const payload = computed(() => props.artifact.metadata.payload as Record<string, unknown>)
const listing = computed(() => (payload.value.listing ?? {}) as Record<string, unknown>)
</script>
<template>
  <dl class="artifact-facts">
    <div><dt>状态</dt><dd>{{ String(payload.status ?? '—') }}</dd></div>
    <div><dt>草稿</dt><dd>{{ String(payload.draft_id ?? payload.id ?? '—') }}</dd></div>
    <div v-if="payload.marketplace"><dt>Marketplace</dt><dd>{{ String(payload.marketplace) }}</dd></div>
    <div v-if="listing.title"><dt>Listing</dt><dd>{{ String(listing.title) }}</dd></div>
  </dl>
</template>
