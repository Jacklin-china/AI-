<script setup lang="ts">
import { computed, ref } from 'vue'

import type { StudioTask } from '../types'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'

const props = defineProps<{ tasks: StudioTask[] }>()
const emit = defineEmits<{ open: [task: StudioTask, path: string]; create: [] }>()
const query = ref('')
const filter = ref('all')
const sort = ref<'created' | 'name' | 'cost'>('created')
function status(task: StudioTask): string { if (task.status === 'failed') return 'failed'; if (task.archived) return 'completed'; if (task.status === 'unknown' || task.has_image) return 'waiting'; return task.status === 'succeeded' ? 'running' : 'pending' }
function stage(task: StudioTask): string { return task.archived ? '交付' : task.review ? '人工审批' : task.qc ? '人工审批' : task.has_image ? '质量检查' : '生成' }
function progress(task: StudioTask): number { return task.archived ? 100 : task.review ? 83 : task.qc ? 67 : task.has_image ? 50 : task.prompt ? 33 : 17 }
const rows = computed(() => [...props.tasks.filter((task) => `${task.project} ${task.request_id}`.toLowerCase().includes(query.value.toLowerCase())).filter((task) => filter.value === 'all' || status(task) === filter.value)].sort((left: StudioTask, right: StudioTask) => sort.value === 'name' ? left.project.localeCompare(right.project) : sort.value === 'cost' ? (right.actual_fen ?? right.estimate_fen) - (left.actual_fen ?? left.estimate_fen) : right.created_at.localeCompare(left.created_at)))
function money(task: StudioTask): string { return `¥${((task.actual_fen ?? task.estimate_fen) / 100).toFixed(2)}` }
function time(raw: string): string { return raw ? raw.slice(0, 16) : '—' }
</script>

<template>
  <main class="page runs-page"><header class="page-header"><div><span class="section-kicker">任务</span><h1>任务与运行</h1><p>搜索、筛选和排序真实生产记录。</p></div><button class="ui-button primary" @click="emit('create')">新建 Run</button></header><div class="table-toolbar"><label class="search-field"><span>⌕</span><input v-model="query" placeholder="搜索 Run 名称或 ID" /></label><select v-model="filter" class="ui-select"><option value="all">全部状态</option><option value="running">运行中</option><option value="waiting">等待</option><option value="completed">已完成</option><option value="failed">失败</option></select><select v-model="sort" class="ui-select"><option value="created">按创建时间</option><option value="name">按名称</option><option value="cost">按成本</option></select><span>{{ rows.length }} 条运行</span></div><section class="run-table"><table><thead><tr><th>运行</th><th>创作域</th><th>工作流</th><th>阶段</th><th>状态</th><th>进度</th><th>耗时</th><th>成本</th><th>创建时间</th></tr></thead><tbody><tr v-for="task in rows" :key="task.request_id" tabindex="0" @click="emit('open', task, `/runs/${encodeURIComponent(task.request_id)}`)" @keydown.enter="emit('open', task, `/runs/${encodeURIComponent(task.request_id)}`)"><td><strong>{{ task.project }} · #{{ task.shot_no }}</strong><small>{{ task.request_id.slice(-8) }}</small></td><td>Studio</td><td>图片生产</td><td>{{ stage(task) }}</td><td><StatusBadge :status="status(task)" /></td><td><span class="progress-cell"><i><b :style="{ width: `${progress(task)}%` }"></b></i>{{ progress(task) }}%</span></td><td>—</td><td>{{ money(task) }}</td><td><time>{{ time(task.created_at) }}</time></td></tr></tbody></table><EmptyState v-if="!rows.length" title="没有匹配的运行" description="调整搜索或筛选条件。" /></section></main>
</template>
