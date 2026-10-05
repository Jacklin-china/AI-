<script setup lang="ts">
import type { CoreRun } from '../types'
import ImageGenerationPlaceholder from './chat/ImageGenerationPlaceholder.vue'
import ChatImageAttachment from './chat/ChatImageAttachment.vue'

defineProps<{
  run: CoreRun | null
  progress?: { visible: boolean; label: string; percent: number } | null
  imageUrl?: string
  disabled: boolean
  imageMode?: 'provider' | 'external'
  preparing?: boolean
  externalPrompt?: Record<string, unknown> | null
  externalImageUrl?: string
  uploading?: boolean
  promptError?: string
}>()
const emit = defineEmits<{ retry: []; resume: []; editPrompt: []; changeModel: []; viewPrompt: []; copyPrompt: []; upload: [file: File]; preview: [media: { url: string; filename: string }] }>()
function selectExternalImage(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) emit('upload', file)
  input.value = ''
}
</script>
<template>
  <div v-if="imageMode === 'external'" class="shot-generation" :data-media-status="externalImageUrl ? 'image_uploaded' : preparing ? 'preparing_prompt' : externalPrompt ? 'awaiting_external_image' : 'prompt_pending'">
    <p v-if="preparing" role="status">正在生成完整 Image Prompt…</p>
    <template v-else-if="externalPrompt">
      <p>Prompt 已生成 · v{{ externalPrompt.version }}{{ externalPrompt.user_confirmed ? ' · 已确认' : '' }}</p>
      <p role="status">{{ uploading ? '正在导入外部图片…' : externalImageUrl ? '外部图片已导入资产库' : '等待外部图片' }}</p>
      <div class="draft-actions">
        <button class="ui-button sm" @click="emit('viewPrompt')">查看完整 Prompt</button>
        <button class="ui-button sm" @click="emit('copyPrompt')">复制生图 Prompt</button>
        <button class="ui-button quiet sm" :disabled="disabled || uploading" @click="emit('editPrompt')">修改 Prompt</button>
        <label class="ui-button sm">上传图片<input type="file" accept="image/png,image/jpeg,image/webp" :disabled="disabled || uploading" @change="selectExternalImage" /></label>
      </div>
      <ChatImageAttachment v-if="externalImageUrl" :media="{ url: externalImageUrl, filename: 'external-image.png' }" @open="emit('preview', $event)" />
    </template>
    <p v-else class="pane-note">生成完整 Image Prompt，复制到 ChatGPT Image 等外部模型后上传图片。</p>
    <p v-if="promptError" role="alert" class="workspace-error">{{ promptError }}</p>
  </div>
  <div v-else class="shot-generation" :data-generation-id="run?.state.request_id" :data-status="run?.status">
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
