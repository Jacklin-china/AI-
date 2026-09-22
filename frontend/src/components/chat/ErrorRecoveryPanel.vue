<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{ message: string }>()
defineEmits<{ retry: [] }>()

const open = ref(false)
const LONG_LIMIT = 120
const isLong = computed(() => props.message.length > LONG_LIMIT)
const shown = computed(() => (isLong.value && !open.value ? `${props.message.slice(0, LONG_LIMIT)}…` : props.message))
</script>
<template>
  <section class="error-recovery" role="alert">
    <div>
      <strong>这次没有完成</strong>
      <p>{{ shown }}</p>
      <button v-if="isLong" type="button" class="text-action" @click="open = !open">{{ open ? '收起' : '查看完整原因' }}</button>
    </div>
    <button type="button" @click="$emit('retry')">重试</button>
  </section>
</template>
