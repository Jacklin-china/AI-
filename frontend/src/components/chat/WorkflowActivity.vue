<script setup lang="ts">
import { ExternalLink } from 'lucide-vue-next'
import type { CoreRun } from '../../types'
defineProps<{ run: CoreRun }>()
defineEmits<{ open: [run: CoreRun] }>()
</script>
<template>
  <section class="workflow-activity">
    <div><span class="activity-dot" :data-status="run.status"></span><strong>生产任务</strong><small>{{ run.current_node }} · {{ run.status }}</small></div>
    <button type="button" @click="$emit('open', run)">打开工作区 <ExternalLink :size="13" /></button>
  </section>
</template>
