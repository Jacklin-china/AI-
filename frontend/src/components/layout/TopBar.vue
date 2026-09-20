<script setup lang="ts">
import { Search } from 'lucide-vue-next'

defineProps<{ title: string; busy: boolean }>()
const emit = defineEmits<{ command: [] }>()
</script>

<template>
  <header class="app-topbar"><div class="topbar-context"><span>Kantoku</span><b>/</b><strong>{{ title }}</strong></div><div class="topbar-actions"><button class="command-trigger" title="打开命令面板 Ctrl+K" @click="emit('command')"><Search :size="13" :stroke-width="2" /><span>搜索命令或页面</span><kbd>Ctrl K</kbd></button><span class="live-state"><i :class="{ running: busy }"></i>{{ busy ? '运行中' : '就绪' }}</span><button class="assistant-chip" title="监督酱 · AI 副导演"><span><img src="/assets/kantoku-avatar.png" alt="监督酱" /></span><b>监督酱</b><small>副导演</small></button></div></header>
</template>
