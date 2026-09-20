<script setup lang="ts">
import StatusBadge from '../components/StatusBadge.vue'
import EmptyState from '../components/EmptyState.vue'
import type { CoreBatch, CoreRun } from '../types'
defineProps<{ batches: CoreBatch[] }>()
const emit = defineEmits<{ open: [run: CoreRun]; cancel: [batch: CoreBatch]; createDemo: [] }>()
function canCancel(batch: CoreBatch): boolean { return ['pending', 'running', 'waiting'].includes(batch.status) }
</script>

<template>
  <main class="page batches-page"><header class="page-header"><div><span class="section-kicker">Batch Runtime</span><h1>批量任务</h1><p>批次状态由成员 Run 聚合；并发上限由 Core 配置控制。</p></div><div class="page-actions"><span class="count-chip">{{ batches.length }} 批次</span><button class="ui-button primary" @click="emit('createDemo')">创建 5 商品 Mock Demo</button></div></header><section v-if="batches.length" class="batch-list"><article v-for="batch in batches" :key="batch.id" class="surface batch-record"><header><div><span class="section-kicker">{{ batch.id }}</span><h2>{{ batch.name }}</h2></div><StatusBadge :status="batch.status" /></header><div class="batch-meta"><span>并发 {{ batch.concurrency_limit }}</span><span>{{ batch.runs.length }} Runs</span><span>外部平台：Mock</span><button v-if="canCancel(batch)" class="text-action danger" @click="emit('cancel', batch)">取消批次</button></div><div class="batch-runs"><button v-for="run in batch.runs" :key="run.id" @click="emit('open', run)"><span><strong>{{ run.id.slice(0, 12) }}</strong><small>{{ run.current_node || run.workflow }}</small></span><span>¥{{ (run.cost_fen / 100).toFixed(2) }}</span><StatusBadge :status="run.status" /></button></div></article></section><EmptyState v-else title="暂无批量任务" description="可创建 5 商品 Mock Demo；Workflow、审批和持久化均为真实 Core，只有外部平台 Adapter 为 Mock。" />
  </main>
</template>
