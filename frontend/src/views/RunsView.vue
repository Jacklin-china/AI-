<script setup lang="ts">
import { computed, ref } from 'vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import type { CoreRun } from '../types'

const props = defineProps<{ runs: CoreRun[] }>()
const emit = defineEmits<{ open: [run: CoreRun]; cancel: [run: CoreRun]; create: [] }>()
const query = ref('')
const status = ref('all')
const domain = ref('all')
const domains = computed(() => [...new Set(props.runs.map((run) => run.domain))].sort())
const rows = computed(() => props.runs.filter((run) => {
  const term = query.value.trim().toLowerCase()
  return (status.value === 'all' || run.status === status.value) && (domain.value === 'all' || run.domain === domain.value) && (!term || `${run.id} ${run.domain} ${run.workflow} ${run.current_node}`.toLowerCase().includes(term))
}))
function formatTime(raw: string): string { const value = new Date(raw); return Number.isNaN(value.valueOf()) ? raw : value.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) }
function progress(run: CoreRun): string { return run.nodes.length ? `${run.nodes.filter((node) => node.status === 'completed').length} / ${run.nodes.length}` : '0 / 0' }
function canCancel(run: CoreRun): boolean { return ['pending', 'running', 'waiting'].includes(run.status) }
</script>

<template>
  <main class="page runs-page"><header class="page-header"><div><span class="section-kicker">Core Runtime</span><h1>任务与运行</h1><p>状态、当前节点与费用均直接来自 Kantoku Core。</p></div><button class="ui-button primary" @click="emit('create')">新建 Production</button></header>
    <div class="table-toolbar"><label class="search-field"><span>⌕</span><input v-model="query" placeholder="搜索 Run、Workflow 或节点" /></label><select v-model="status" class="ui-select"><option value="all">全部状态</option><option v-for="item in ['pending','running','waiting','completed','failed','cancelled']" :key="item" :value="item">{{ item }}</option></select><select v-model="domain" class="ui-select"><option value="all">全部 Domain</option><option v-for="item in domains" :key="item" :value="item">{{ item }}</option></select><span>{{ rows.length }} 个真实 Run</span></div>
    <section v-if="rows.length" class="run-table"><table><thead><tr><th>Run</th><th>Domain / Workflow</th><th>状态</th><th>当前节点</th><th>节点进度</th><th>费用</th><th>更新时间</th><th>操作</th></tr></thead><tbody><tr v-for="run in rows" :key="run.id" @click="emit('open', run)"><td><strong>{{ run.id.slice(0, 12) }}</strong><small>{{ run.id }}</small></td><td><strong>{{ run.domain }}</strong><small>{{ run.workflow }}</small></td><td><StatusBadge :status="run.status" /></td><td>{{ run.current_node || '—' }}</td><td>{{ progress(run) }}</td><td>¥{{ (run.cost_fen / 100).toFixed(2) }}</td><td><time>{{ formatTime(run.updated_at) }}</time></td><td><button v-if="canCancel(run)" class="text-action danger" @click.stop="emit('cancel', run)">取消</button><button v-else class="text-action" @click.stop="emit('open', run)">查看</button></td></tr></tbody></table></section>
    <EmptyState v-else title="没有符合条件的运行" description="创建 Commerce 或 Comic Production 后，Core Run 会显示在这里。" />
  </main>
</template>
