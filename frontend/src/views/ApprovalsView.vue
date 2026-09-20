<script setup lang="ts">
import { computed, ref } from 'vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import type { CoreApproval, CoreRun } from '../types'

const props = defineProps<{ approvals: CoreApproval[]; runs: CoreRun[]; busy: boolean }>()
const emit = defineEmits<{ decide: [approval: CoreApproval, action: 'approve' | 'reject' | 'revise']; open: [runId: string] }>()
const tab = ref<'pending' | 'history'>('pending')
const runMap = computed(() => new Map(props.runs.map((run) => [run.id, run])))
const rows = computed(() => props.approvals.filter((item) => tab.value === 'pending' ? item.decision === 'pending' : item.decision !== 'pending'))
</script>

<template>
  <main class="page approvals-page"><header class="page-header"><div><span class="section-kicker">Human in the loop</span><h1>审批</h1><p>一次决定由后端持久化并恢复原 Run，前端不会重复触发 resume。</p></div><span class="count-chip">{{ approvals.filter((item) => item.decision === 'pending').length }} 待处理</span></header><div class="detail-tabs"><button :class="{ active: tab === 'pending' }" @click="tab = 'pending'">待处理</button><button :class="{ active: tab === 'history' }" @click="tab = 'history'">审批历史</button></div>
    <section v-if="rows.length" class="approval-list"><article v-for="approval in rows" :key="approval.id" class="surface approval-record"><header><div><span class="section-kicker">{{ runMap.get(approval.run_id)?.domain ?? 'Core' }} · {{ approval.node_id }}</span><h2>{{ String(approval.request.title ?? approval.request.stage ?? '人工审批') }}</h2></div><StatusBadge :status="approval.decision === 'pending' ? 'waiting' : approval.decision" :label="approval.decision" /></header><pre>{{ JSON.stringify(approval.request, null, 2) }}</pre><footer><button class="text-action" @click="emit('open', approval.run_id)">查看 Run</button><template v-if="approval.decision === 'pending'"><button class="ui-button" :disabled="busy" @click="emit('decide', approval, 'reject')">拒绝</button><button class="ui-button" :disabled="busy" @click="emit('decide', approval, 'revise')">要求修改</button><button class="ui-button primary" :disabled="busy" @click="emit('decide', approval, 'approve')">批准并继续</button></template></footer></article></section><EmptyState v-else :title="tab === 'pending' ? '没有待审批事项' : '暂无审批历史'" description="Workflow 到达 Approval Node 时会自动出现在这里。" />
  </main>
</template>
