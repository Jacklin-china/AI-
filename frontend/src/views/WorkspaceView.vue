<script setup lang="ts">
import { Plus } from 'lucide-vue-next'
import { Pane, Splitpanes } from 'splitpanes'
import 'splitpanes/dist/splitpanes.css'

import type { DomainDefinition } from '../domains'
import type { GenerationSettings, StudioBudget, StudioConfig, StudioForm, StudioTask } from '../types'
import type { WorkflowStep } from '../components/WorkflowProgress.vue'
import StatusBadge from '../components/StatusBadge.vue'
import WorkflowProgress from '../components/WorkflowProgress.vue'
import CreationChat from '../components/workspace/CreationChat.vue'
import InspectorPanel from '../components/workspace/InspectorPanel.vue'

export interface Candidate { task: StudioTask; url?: string }
defineProps<{ domain: DomainDefinition; form: StudioForm; generation: GenerationSettings; task: StudioTask | null; busy: boolean; approved: boolean; version: number; workflow: WorkflowStep[]; budget: StudioBudget; config: StudioConfig; estimatedFen: number; artworkUrl?: string; candidates: Candidate[] }>()
const emit = defineEmits<{ reset: []; switchStudio: []; compose: []; refine: []; approve: []; generate: []; recover: []; resume: []; precheck: []; decide: [decision: 'approve' | 'reject' | 'request_revision', reason: string, notes: string]; archive: []; openCandidate: [task: StudioTask] }>()
function money(value: number | null): string { return value === null ? '—' : `¥${(value / 100).toFixed(2)}` }
</script>

<template>
  <main class="workspace-page dense-workspace">
    <header class="workspace-toolbar"><div><span class="section-kicker">{{ domain.label }} · 工作台</span><h1>{{ form.project }}</h1></div><div><StatusBadge :status="domain.status" /><button class="ui-button" @click="emit('reset')"><Plus :size="13" :stroke-width="2" />新建任务</button></div></header>
    <div v-if="domain.status !== 'available'" class="preview-banner"><div><strong>{{ domain.name }} Domain 当前为 Preview</strong><p>尚未连接可执行 Workflow，不会伪造运行。</p></div><button class="ui-button primary" @click="emit('switchStudio')">切换到 Studio</button></div>
    <Splitpanes class="workspace-splitpanes">
      <Pane :size="19" :min-size="15" :max-size="28"><aside class="workflow-pane"><header class="panel-header"><div><span class="section-kicker">工作流</span><h2>图片生产</h2></div></header><WorkflowProgress :steps="workflow" /><div class="cost-widget"><span>项目用量</span><strong>{{ money(budget.settled_fen + budget.held_fen) }}</strong><small>上限 {{ money(budget.limit_fen) }} · 可用 {{ money(budget.available_fen) }}</small><i><b :style="{ width: `${budget.limit_fen ? Math.min(100, ((budget.settled_fen + budget.held_fen) / budget.limit_fen) * 100) : 0}%` }"></b></i></div></aside></Pane>
      <Pane :size="53" :min-size="38"><CreationChat :form="form" :generation="generation" :version="version" :approved="approved" :busy="busy" :task="task" :artwork-url="artworkUrl" :candidates="candidates" :image-model="config.image" :estimate="money(estimatedFen)" @compose="emit('compose')" @refine="emit('refine')" @approve="emit('approve')" @generate="emit('generate')" @recover="emit('recover')" @resume="emit('resume')" @open-candidate="(item) => emit('openCandidate', item)" /></Pane>
      <Pane :size="28" :min-size="22" :max-size="38"><InspectorPanel :form="form" :settings="generation" :task="task" :busy="busy" :image-model="config.image" :image-size="config.size" :estimate="money(estimatedFen)" :artwork-url="artworkUrl" @precheck="emit('precheck')" @decide="(decision, reason, notes) => emit('decide', decision, reason, notes)" @archive="emit('archive')" /></Pane>
    </Splitpanes>
  </main>
</template>
