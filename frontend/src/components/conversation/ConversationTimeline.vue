<script setup lang="ts">
import ApprovalCard from '../approval/ApprovalCard.vue'
import ArtifactCard from '../artifacts/ArtifactCard.vue'
import type { CoreApproval, CoreArtifact } from '../../types'

export interface TimelineItem {
  key: string
  kind: 'user' | 'assistant' | 'progress' | 'error' | 'approval' | 'artifact' | 'done'
  text: string
  at: string | null
  approval?: CoreApproval
  artifact?: CoreArtifact
}

defineProps<{ items: TimelineItem[]; domain: string; busy: boolean }>()
const emit = defineEmits<{ decide: [approval: CoreApproval, action: 'approve' | 'reject' | 'revise', response: Record<string, unknown>] }>()

function clock(raw: string | null): string {
  if (!raw) return ''
  const value = new Date(String(raw).replace(' ', 'T') + 'Z')
  return Number.isNaN(value.valueOf()) ? '' : value.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <ol class="conversation-timeline">
    <li v-for="item in items" :key="item.key" :class="`timeline-${item.kind}`">
      <div v-if="item.kind === 'user'" class="bubble user-bubble">
        <span class="avatar user-avatar">你</span>
        <div><p>{{ item.text }}</p><time>{{ clock(item.at) }}</time></div>
      </div>

      <div v-else-if="item.kind === 'assistant'" class="bubble assistant-bubble">
        <span class="avatar bot-avatar">监督</span>
        <div><p>{{ item.text }}</p><time>{{ clock(item.at) }}</time></div>
      </div>

      <div v-else-if="item.kind === 'progress'" class="progress-line">
        <i aria-hidden="true"></i><span>{{ item.text }}</span>
      </div>

      <div v-else-if="item.kind === 'error'" class="error-line" role="alert">{{ item.text }}</div>

      <div v-else-if="item.kind === 'done'" class="done-line"><span>✓</span>{{ item.text }}</div>

      <ApprovalCard
        v-else-if="item.kind === 'approval' && item.approval"
        :approval="item.approval"
        :domain="domain"
        :busy="busy"
        @decide="(action, response) => emit('decide', item.approval!, action, response)"
      />

      <ArtifactCard v-else-if="item.kind === 'artifact' && item.artifact" :artifact="item.artifact" :domain="domain" />
    </li>
  </ol>
</template>
