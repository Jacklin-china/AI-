<script setup lang="ts">
import { ref } from 'vue'
import { ImagePlus } from 'lucide-vue-next'

const props = withDefaults(defineProps<{ disabled?: boolean; uploading?: boolean; label?: string }>(), {
  disabled: false, uploading: false, label: '导入图片',
})
const emit = defineEmits<{ upload: [file: File] }>()
const input = ref<HTMLInputElement | null>(null)
const dragDepth = ref(0)
const error = ref('')

function select(files: FileList | File[] | null): void {
  if (props.disabled || props.uploading) return
  error.value = ''
  if (!files?.length) return
  if (files.length !== 1) { error.value = '请每次导入一张图片'; return }
  const file = files[0]!
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
    error.value = '请选择 PNG、JPEG 或 WebP 图片'; return
  }
  if (!file.size || file.size > 5 * 1024 * 1024) {
    error.value = '请选择不超过 5 MB 的图片'; return
  }
  emit('upload', file)
}
function choose(event: Event): void {
  const target = event.target as HTMLInputElement
  select(target.files)
  target.value = ''
}
function enter(event: DragEvent): void {
  if (!props.disabled && !props.uploading && event.dataTransfer?.types.includes('Files')) dragDepth.value++
}
function drop(event: DragEvent): void {
  dragDepth.value = 0
  select(event.dataTransfer?.files ?? null)
}
</script>

<template>
  <div class="image-import" :class="{ 'is-dragging': dragDepth > 0 }" :aria-busy="uploading"
    @dragenter.prevent="enter" @dragover.prevent @dragleave.prevent="dragDepth = Math.max(0, dragDepth - 1)" @drop.prevent="drop">
    <slot />
    <div class="image-import-controls">
      <input ref="input" type="file" accept="image/png,image/jpeg,image/webp" :disabled="disabled || uploading" aria-label="选择导入图片" hidden @change="choose" />
      <button class="ui-button sm" :disabled="disabled || uploading" @click="input?.click()"><ImagePlus :size="14" /> {{ uploading ? '正在导入…' : label }}</button>
      <small>{{ dragDepth > 0 ? '松开鼠标导入图片' : '或将图片拖到此处 · PNG/JPEG/WebP · ≤ 5 MB' }}</small>
      <!-- 导入区同时承载"删掉看走眼的那张"，由调用方提供，组件本身不猜业务语义。 -->
      <slot name="actions" />
    </div>
    <p v-if="error" class="workspace-error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.image-import { border: 1px dashed var(--border-strong); border-radius: var(--radius-control); padding: var(--space-3); transition: border-color var(--duration-fast); }
.image-import.is-dragging { border-color: var(--accent); background: var(--accent-soft); outline: 2px solid var(--accent-ring); }
.image-import-controls { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-3); }
.image-import-controls small { color: var(--text-muted); font-size: var(--font-kicker); }
.image-import:has(> :not(.image-import-controls):not(p)) .image-import-controls { margin-top: var(--space-3); }
</style>
