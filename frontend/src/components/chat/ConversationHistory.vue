<script setup lang="ts">
import { ref } from 'vue'
import { Pencil, Trash2 } from 'lucide-vue-next'
import type { Conversation } from '../../types'

defineProps<{ conversations: Conversation[]; activeId: string; busy: boolean; listOnly?: boolean }>()
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
  <aside class="conversation-history" :class="{ 'history-rail': !listOnly }" aria-label="历史聊天">
    <button v-if="!listOnly" class="history-new" type="button" :disabled="busy" @click="emit('create')">＋ 新建对话</button>
    <label class="conversation-search"><span class="sr-only">搜索对话</span><input v-model="search" type="search" placeholder="搜索对话" /></label>
    <div class="conversation-list" tabindex="0" aria-label="最近对话列表">
      <div v-for="item in conversations.filter((entry) => entry.title.toLowerCase().includes(search.trim().toLowerCase()))" :key="item.id" class="conversation-row" :data-active="item.id === activeId">
        <input v-if="editing === item.id" v-model="title" class="history-rename" :aria-label="`重命名 ${item.title}`" @keyup.enter="finishRename" @keyup.esc="editing = ''" @blur="finishRename" />
        <button v-else type="button" class="conversation-select" :title="item.title" :aria-current="item.id === activeId ? 'true' : undefined" :disabled="busy" @click="emit('select', item.id)">{{ item.title }}</button>
        <span class="conversation-actions">
          <button type="button" :aria-label="`重命名 ${item.title}`" :disabled="busy" @click="startRename(item)"><Pencil :size="13" /></button>
          <button type="button" :aria-label="`删除 ${item.title}`" :disabled="busy" @click="emit('remove', item.id)"><Trash2 :size="13" /></button>
        </span>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.conversation-history { display:flex; flex:1; flex-direction:column; min-height:0; min-width:0; }
.conversation-search { display:block; flex-shrink:0; margin-bottom:8px; }
.conversation-search input, .history-rename { width:100%; min-width:0; padding:8px 10px; border:1px solid var(--border-muted); border-radius:12px; background:var(--surface); color:var(--text-primary); font:inherit; font-size:12px; }
.conversation-list { flex:1; min-height:0; overflow-y:auto; overscroll-behavior:contain; scrollbar-width:thin; }
.conversation-row { display:flex; align-items:center; min-height:36px; border-radius:12px; }
.conversation-row:hover { background:var(--sidebar-hover); }
.conversation-row[data-active="true"] { background:var(--sidebar-selected); }
.conversation-select { flex:1; min-width:0; padding:9px 10px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; border:0; border-radius:12px; background:transparent; color:var(--text-primary); text-align:left; font:inherit; font-size:12px; cursor:pointer; }
.conversation-actions { display:flex; flex-shrink:0; gap:2px; padding-right:4px; opacity:0; }
.conversation-row:hover .conversation-actions, .conversation-row:focus-within .conversation-actions { opacity:1; }
.conversation-actions button { display:grid; place-items:center; width:24px; height:26px; border:0; border-radius:8px; background:transparent; color:var(--text-muted); cursor:pointer; }
.conversation-actions button:hover { background:var(--surface); color:var(--text-primary); }
:is(input, button):focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
@media(hover:none) { .conversation-actions { opacity:1; } }
</style>
