<script setup lang="ts">
import { Download, Maximize2 } from 'lucide-vue-next'

const props = defineProps<{ media: { url: string; filename: string } }>()
const emit = defineEmits<{ open: [media: { url: string; filename: string }] }>()
</script>

<template>
  <figure class="chat-generated-image chat-image-attachment">
    <div class="chat-image-surface">
      <button type="button" class="chat-image-open" aria-label="放大查看生成的图片" @click="emit('open', props.media)">
        <img :src="media.url" alt="此聊天生成的图片" />
      </button>
      <div class="chat-image-toolbar" aria-label="图片操作">
        <button type="button" class="chat-image-tool" aria-label="放大查看" @click="emit('open', props.media)">
          <Maximize2 :size="17" :stroke-width="1.8" aria-hidden="true" />
          <span class="chat-image-tooltip" role="tooltip">放大查看</span>
        </button>
        <a class="chat-image-tool" :href="media.url" :download="media.filename" aria-label="下载原图">
          <Download :size="17" :stroke-width="1.8" aria-hidden="true" />
          <span class="chat-image-tooltip" role="tooltip">下载原图</span>
        </a>
      </div>
    </div>
  </figure>
</template>
