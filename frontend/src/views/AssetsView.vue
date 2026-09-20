<script setup lang="ts">
import { computed, ref } from 'vue'

import type { StudioTask } from '../types'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'

const props = defineProps<{ tasks: StudioTask[]; artworkUrls: Record<string, string> }>()
const emit = defineEmits<{ open: [task: StudioTask] }>()
const tab = ref('all')
const view = ref<'grid' | 'list'>('grid')
const query = ref('')
const preview = ref<StudioTask | null>(null)

const emptyTabs = ['videos', 'documents', 'other']
const rows = computed(() => props.tasks.filter((task) => tab.value === 'prompts' ? true : task.has_image).filter((task) => `${task.project} ${task.prompt}`.toLowerCase().includes(query.value.toLowerCase())))

/* ---- 按天分组：对齐参考稿的「大号日期标题 + 密集缩略图墙」 ---- */
function dayKey(raw: string): string { return (raw || '').slice(0, 10) || 'unknown' }
function dayLabel(key: string): string {
  const parts = key.split('-')
  const month = Number(parts[1])
  const day = Number(parts[2])
  if (parts.length < 3 || Number.isNaN(month) || Number.isNaN(day)) return '未记录日期'
  return `${month}月${day}日`
}
const groups = computed(() => {
  const buckets = new Map<string, StudioTask[]>()
  for (const task of rows.value) {
    const key = dayKey(task.created_at)
    const bucket = buckets.get(key)
    if (bucket) bucket.push(task)
    else buckets.set(key, [task])
  }
  return [...buckets.entries()].sort((a, b) => b[0].localeCompare(a[0])).map(([key, items]) => ({ key, label: dayLabel(key), items }))
})
function tip(task: StudioTask): string { return `${task.project} · #${task.shot_no}\n${task.prompt.slice(0, 70)}` }
function kind(): string { return tab.value === 'prompts' ? '提示词' : '图片' }
</script>

<template>
  <main class="page assets-page">
    <header class="page-header"><div><span class="section-kicker">资产库</span><h1>资产</h1><p>搜索、筛选并预览真实 Artifact。</p></div></header>
    <div class="asset-toolbar"><div class="tabs"><button v-for="item in [['all','全部'],['images','图片'],['videos','视频'],['documents','文档'],['prompts','提示词'],['other','其他']]" :key="item[0]" :class="{ active: tab === item[0] }" @click="tab = item[0]">{{ item[1] }}</button></div><label class="search-field"><span>⌕</span><input v-model="query" placeholder="搜索资产或 Prompt" /></label><div class="view-toggle"><button :class="{ active: view === 'grid' }" @click="view = 'grid'">▦</button><button :class="{ active: view === 'list' }" @click="view = 'list'">☷</button></div></div>

    <template v-if="rows.length && !emptyTabs.includes(tab)">
      <template v-if="view === 'grid'">
        <section v-for="group in groups" :key="group.key" class="asset-day">
          <h2 class="asset-day-title">{{ group.label }}<small>{{ group.items.length }} 张</small></h2>
          <div class="asset-wall">
            <button v-for="task in group.items" :key="task.request_id" class="asset-tile" :data-tip="tip(task)" @click="preview = task">
              <span class="asset-tile-media"><img v-if="tab !== 'prompts' && artworkUrls[task.request_id]" :src="artworkUrls[task.request_id]" alt="Asset preview" /><p v-else>{{ task.prompt }}</p><span class="asset-tile-meta"><strong>{{ task.project }}</strong><small>{{ kind() }} · #{{ task.shot_no }}</small></span></span>
              <StatusBadge class="asset-tile-badge" :status="task.archived ? 'completed' : 'waiting'" />
            </button>
          </div>
        </section>
      </template>
      <section v-else class="asset-library-list"><button v-for="task in rows" :key="task.request_id" @click="preview = task"><div class="asset-preview"><img v-if="tab !== 'prompts' && artworkUrls[task.request_id]" :src="artworkUrls[task.request_id]" alt="Asset preview" /><p v-else>{{ task.prompt }}</p></div><div class="asset-info"><strong>{{ task.project }}</strong><small>{{ kind() }} · Studio · #{{ task.shot_no }}</small><StatusBadge :status="task.archived ? 'completed' : 'waiting'" /></div></button></section>
    </template>
    <EmptyState v-else title="该分类暂无资产" description="未接入的类型保持为空，不使用伪造数据。" />

    <div v-if="preview" class="asset-preview-backdrop" @click.self="preview = null"><section class="asset-preview-dialog"><header><div><span class="section-kicker">资产预览</span><h2>{{ preview.project }}</h2></div><button @click="preview = null">×</button></header><div class="asset-preview-stage"><img v-if="artworkUrls[preview.request_id]" :src="artworkUrls[preview.request_id]" alt="Full asset preview" /><p v-else>{{ preview.prompt }}</p></div><footer><div><span>运行</span><b>{{ preview.request_id }}</b></div><div><span>状态</span><StatusBadge :status="preview.archived ? 'completed' : 'waiting'" /></div><button class="ui-button primary" @click="emit('open', preview)">在 Workspace 打开</button></footer></section></div>
  </main>
</template>
