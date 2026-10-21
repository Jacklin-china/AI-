<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { CoreArtifact } from '../../types'
import { artifactFilename, artifactOrigin, loadArtifactMedia } from './artifactPresentation'

const props = defineProps<{ artifact: CoreArtifact; detailed?: boolean }>()
const origin = computed(() => artifactOrigin(props.artifact))
const mediaUrl = ref('')
const loading = ref(false)
const error = ref('')
let selection = 0
function release(): void {
  if (mediaUrl.value) URL.revokeObjectURL(mediaUrl.value)
  mediaUrl.value = ''
}
async function load(): Promise<void> {
  const current = ++selection
  release()
  error.value = ''
  loading.value = false
  if (['mock', 'blocked'].includes(origin.value)) return
  if (!props.artifact.location && props.artifact.source !== 'comic.archive') return
  loading.value = true
  try {
    const url = await loadArtifactMedia(props.artifact)
    if (current !== selection) { URL.revokeObjectURL(url); return }
    mediaUrl.value = url
  } catch (failure) {
    if (current === selection) error.value = failure instanceof Error ? failure.message : '媒体读取失败，请稍后重试。'
  } finally { if (current === selection) loading.value = false }
}
function decodeFailed(): void {
  release()
  error.value = props.artifact.type === 'video' ? '浏览器无法播放该视频，文件可能损坏或格式不受支持。' : '图片无法显示，文件可能损坏或格式不受支持。'
}
watch(() => props.artifact, () => void load(), { immediate: true })
onBeforeUnmount(() => { selection++; release() })
</script>

<template>
  <div class="artifact-media" :class="{ 'artifact-media-detail': detailed }">
    <template v-if="mediaUrl">
      <img v-if="artifact.type === 'image'" :src="mediaUrl" alt="资产图片预览" @error="decodeFailed" />
      <video v-else :src="mediaUrl" :controls="detailed" preload="metadata" aria-label="资产视频预览" @error="decodeFailed" />
      <a v-if="detailed" class="ui-button sm" :href="mediaUrl" :download="artifactFilename(artifact, artifact.type === 'image' ? '图片' : '视频')">{{ artifact.type === 'image' ? '下载原图' : '下载视频' }}</a>
    </template>
    <p v-else-if="loading" role="status">正在加载{{ artifact.type === 'image' ? '图片' : '视频' }}…</p>
    <div v-else-if="error" role="alert"><p>{{ error }}</p><button class="ui-button sm" @click="load">重新读取文件</button></div>
    <p v-else-if="origin === 'mock'">模拟数据（Mock），未生成真实{{ artifact.type === 'image' ? '图片' : '视频' }}。</p>
    <p v-else-if="origin === 'blocked'">媒体生成已阻止，没有可交付的真实文件。</p>
    <p v-else>尚未关联可读取的媒体文件。</p>
  </div>
</template>

<style scoped>
.artifact-media { display: grid; gap: 12px; margin-top: 12px; color: var(--text-secondary); font-size: 13px; }
.artifact-media img, .artifact-media video { display: block; width: 100%; height: auto; max-height: 200px; object-fit: contain; background: var(--surface-subtle); border-radius: 8px; }
.artifact-media-detail img, .artifact-media-detail video { max-height: 60vh; }
.artifact-media p { margin: 0; line-height: 1.6; overflow-wrap: anywhere; }
.artifact-media a { justify-self: start; }
</style>
