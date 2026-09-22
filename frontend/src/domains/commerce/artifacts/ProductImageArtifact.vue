<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ImageOff, Loader2 } from 'lucide-vue-next'
import { getArtifactContentUrl } from '../../../services/core'
import type { CoreArtifact } from '../../../types'
const props = defineProps<{ artifact: CoreArtifact }>()
const payload = computed(() => props.artifact.metadata.payload as Record<string, unknown>)
const origin = computed(() => String(props.artifact.metadata.origin ?? 'mock'))
const imageUrl = ref('')
const loading = ref(false)
onMounted(async () => {
  if (origin.value !== 'real' || !props.artifact.location) return
  loading.value = true
  imageUrl.value = await getArtifactContentUrl(props.artifact.id) ?? ''
  loading.value = false
})
onBeforeUnmount(() => { if (imageUrl.value) URL.revokeObjectURL(imageUrl.value) })
</script>
<template>
  <div v-if="imageUrl" class="product-image-preview"><img :src="imageUrl" alt="已生成的商品主图" /></div>
  <div v-else class="product-image-placeholder" :data-origin="origin">
    <Loader2 v-if="loading" :size="20" class="spin" />
    <ImageOff v-else :size="20" />
    <strong>{{ origin === 'mock' ? 'Mock 商品主图' : origin === 'blocked' ? '真实生图已阻止' : '图片不可用' }}</strong>
    <p>{{ String(payload.message ?? (origin === 'mock' ? '未生成真实图片' : '请查看详情或重试')) }}</p>
  </div>
  <dl class="artifact-facts image-brief-facts">
    <div><dt>用途</dt><dd>Marketplace 商品主图</dd></div>
    <div><dt>模型</dt><dd>{{ String(payload.model ?? '—') }}</dd></div>
  </dl>
</template>
