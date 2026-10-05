<script setup lang="ts">
import type { CoreRun } from '../types'
import ImageGenerationPlaceholder from './chat/ImageGenerationPlaceholder.vue'
import ChatImageAttachment from './chat/ChatImageAttachment.vue'

defineProps<{
  run: CoreRun | null
  progress?: { visible: boolean; label: string; percent: number } | null
  imageUrl?: string
  disabled: boolean
}>()
const emit = defineEmits<{ retry: []; resume: []; editPrompt: []; changeModel: []; preview: [media: { url: string; filename: string }] }>()
</script>
<template>
  <div class="shot-generation" :data-generation-id="run?.state.request_id" :data-status="run?.status">
    <ImageGenerationPlaceholder v-if="progress?.visible" :label="progress.label" :percent="progress.percent" :started-at="run?.started_at" />
    <ChatImageAttachment v-if="imageUrl && run?.state.image_artifact_id" :media="{ url: imageUrl, filename: `comic-${run.state.image_artifact_id}.png` }" @open="emit('preview', $event)" />
    <p v-if="run?.status === 'failed'" class="workspace-error" role="alert">当前镜头生成失败：{{ run.error }} · {{ run.state.error_id }}</p>
    <div v-if="run?.status === 'failed' || run?.status === 'waiting'" class="draft-actions">
      <button v-if="run.image_execution?.can_resume" class="ui-button sm" :disabled="disabled" @click="emit('resume')">继续查询原任务</button>
      <button v-if="run.image_execution?.can_regenerate" class="ui-button sm" :disabled="disabled" @click="emit('retry')">重新生成</button>
      <button class="ui-button quiet sm" :disabled="disabled || run.image_execution?.needs_reconciliation" @click="emit('editPrompt')">修改 Prompt</button>
      <button class="ui-button quiet sm" @click="emit('changeModel')">更换模型</button>
      <span v-if="run.image_execution?.needs_reconciliation">原请求账单未知，需先对账。</span>
    </div>
    <p v-if="run?.image_execution && ['completed', 'failed'].includes(run.status)" class="pane-note">模型：{{ run.image_execution.model }} · 图片成本：{{ run.image_execution.actual_fen === null ? '待结算' : `¥${(run.image_execution.actual_fen / 100).toFixed(2)}` }}</p>
  </div>
</template>
