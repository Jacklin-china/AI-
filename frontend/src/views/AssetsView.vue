<script setup lang="ts">
import { computed, ref } from 'vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import type { CoreArtifact, CoreRun } from '../types'

const props = defineProps<{ artifacts: CoreArtifact[]; runs: CoreRun[] }>()
const emit = defineEmits<{ open: [runId: string] }>()
const tab = ref('all')
const query = ref('')
const preview = ref<CoreArtifact | null>(null)
const runMap = computed(() => new Map(props.runs.map((run) => [run.id, run])))
const rows = computed(() => props.artifacts.filter((artifact) => {
  const matchesType = tab.value === 'all' || artifact.type === tab.value || (tab.value === 'document' && ['document', 'json', 'report', 'listing'].includes(artifact.type))
  const term = query.value.trim().toLowerCase()
  return matchesType && (!term || `${artifact.id} ${artifact.type} ${artifact.source} ${artifact.run_id} ${JSON.stringify(artifact.metadata)}`.toLowerCase().includes(term))
}))
function domainOf(artifact: CoreArtifact): string { return runMap.value.get(artifact.run_id)?.domain ?? '—' }
function summary(artifact: CoreArtifact): string { return String(artifact.metadata.title ?? artifact.metadata.prompt ?? artifact.metadata.name ?? artifact.location ?? artifact.source).slice(0, 120) }
</script>

<template>
  <main class="page assets-page"><header class="page-header"><div><span class="section-kicker">Artifact Registry</span><h1>资产</h1><p>仅展示 Core 持久化的真实 Artifact，不生成占位素材。</p></div><span class="count-chip">{{ artifacts.length }} 项</span></header>
    <div class="asset-toolbar"><div class="tabs"><button v-for="item in [['all','全部'],['image','图片'],['video','视频'],['prompt','Prompt'],['listing','Listing'],['document','文档与报告']]" :key="item[0]" :class="{ active: tab === item[0] }" @click="tab = item[0]">{{ item[1] }}</button></div><label class="search-field"><span>⌕</span><input v-model="query" placeholder="搜索 Artifact" /></label></div>
    <section v-if="rows.length" class="artifact-grid"><button v-for="artifact in rows" :key="artifact.id" class="surface artifact-record" @click="preview = artifact"><header><span class="artifact-kind">{{ artifact.type.toUpperCase() }}</span><StatusBadge :status="artifact.status" /></header><strong>{{ summary(artifact) }}</strong><p>{{ artifact.id }}</p><footer><span>{{ domainOf(artifact) }} · {{ artifact.node_id }}</span><span>v{{ artifact.version }}</span></footer></button></section>
    <EmptyState v-else title="该分类暂无资产" description="Artifact 会在真实 Workflow 节点完成后进入注册表。" />
    <div v-if="preview" class="asset-preview-backdrop" @click.self="preview = null"><section class="asset-preview-dialog artifact-dialog"><header><div><span class="section-kicker">Artifact 详情</span><h2>{{ preview.type }} · v{{ preview.version }}</h2></div><button @click="preview = null">×</button></header><dl><div><dt>ID</dt><dd>{{ preview.id }}</dd></div><div><dt>Run</dt><dd>{{ preview.run_id }}</dd></div><div><dt>Node</dt><dd>{{ preview.node_id }}</dd></div><div><dt>Source</dt><dd>{{ preview.source }}</dd></div><div><dt>Location</dt><dd>{{ preview.location ?? '—' }}</dd></div></dl><pre>{{ JSON.stringify(preview.metadata, null, 2) }}</pre><footer><StatusBadge :status="preview.status" /><button class="ui-button primary" @click="emit('open', preview.run_id)">查看 Run</button></footer></section></div>
  </main>
</template>
