<script setup lang="ts">
import { ref } from 'vue'
import type { Conversation } from '../../types'

defineProps<{ conversations: Conversation[]; activeId: string; busy: boolean }>()
const emit = defineEmits<{
  create: []
  select: [id: string]
  rename: [id: string, title: string]
  remove: [id: string]
}>()
const search = ref('')
const editing = ref('')
const title = ref('')

function startRename(item: Conversation): void {
  editing.value = item.id
  title.value = item.title
}
function finishRename(): void {
  if (editing.value && title.value.trim()) emit('rename', editing.value, title.value.trim())
  editing.value = ''
}
</script>

<template>
  <aside class="history-rail" aria-label="历史聊天">
    <button class="history-new" type="button" :disabled="busy" @click="emit('create')">＋ 新建聊天</button>
    <label class="history-search"><span class="sr-only">搜索聊天</span><input v-model="search" type="search" placeholder="搜索聊天" /></label>
    <div class="history-list">
      <div v-for="item in conversations.filter((entry) => entry.title.toLowerCase().includes(search.toLowerCase()))" :key="item.id" class="history-item" :data-active="item.id === activeId">
        <input v-if="editing === item.id" v-model="title" class="history-rename" :aria-label="`重命名 ${item.title}`" @keyup.enter="finishRename" @keyup.esc="editing = ''" @blur="finishRename" />
        <button v-else type="button" class="history-select" :title="item.title" @click="emit('select', item.id)">{{ item.title }}</button>
        <button type="button" class="history-action" :aria-label="`重命名 ${item.title}`" :disabled="busy" @click="startRename(item)">✎</button>
        <button type="button" class="history-action" :aria-label="`删除 ${item.title}`" :disabled="busy" @click="emit('remove', item.id)">×</button>
      </div>
    </div>
  </aside>
</template>
