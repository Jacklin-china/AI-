<script setup lang="ts">
import { Check, Loader2, RefreshCw, RotateCcw, Search, Send, ShieldCheck, Zap } from 'lucide-vue-next'
import { computed } from 'vue'

import type { GenerationSettings, StudioForm, StudioTask } from '../../types'
import StatusBadge from '../StatusBadge.vue'

export interface Candidate { task: StudioTask; url?: string }
const props = defineProps<{ form: StudioForm; generation: GenerationSettings; version: number; approved: boolean; busy: boolean; task: StudioTask | null; artworkUrl?: string; candidates: Candidate[]; imageModel: string; estimate: string }>()
const emit = defineEmits<{ compose: []; refine: []; approve: []; generate: []; recover: []; resume: []; openCandidate: [task: StudioTask] }>()

const hasSubject = computed(() => !!props.form.subject.trim())
const hasPrompt = computed(() => !!props.form.prompt.trim())
const showPlan = computed(() => hasPrompt.value && props.approved)
const showResult = computed(() => !!props.task?.has_image)
const planRows = computed(() => [
  { label: '模型', value: props.generation.model || props.imageModel },
  { label: '比例', value: props.generation.ratio },
  { label: '分辨率', value: props.generation.resolution },
  { label: '数量', value: `${props.generation.quantity} 张` },
  { label: '预计费用', value: props.estimate },
])
</script>

<template>
  <section class="creation-chat">
    <details class="chat-context"><summary>任务上下文 · {{ form.project }} · 镜头 #{{ form.shot_no }}</summary><div class="form-grid"><label>项目名称<input v-model="form.project" class="ui-input" /></label><label>镜头 / 单元<input v-model.number="form.shot_no" class="ui-input" type="number" min="1" /></label><label>用途<input v-model="form.purpose" class="ui-input" /></label><label>受众<input v-model="form.audience" class="ui-input" /></label><label class="span-two">风格<input v-model="form.style" class="ui-input" /></label></div></details>

    <div class="chat-list">
      <div v-if="!hasSubject" class="chat-empty"><p>在下方描述你的创作需求，监督酱会整理 Prompt、估价并在你确认后执行。</p></div>

      <div v-if="hasSubject" class="msg-row user"><div class="msg-bubble"><p>{{ form.subject }}</p><small>{{ form.project }} · 镜头 #{{ form.shot_no }}</small></div></div>

      <div v-if="hasPrompt" class="msg-row assistant">
        <span class="msg-avatar"><img src="/assets/kantoku-avatar.png" alt="监督酱" /></span>
        <div class="msg-card">
          <header><strong>已按需求整理 Prompt</strong><StatusBadge :status="approved ? 'completed' : 'draft'" :label="approved ? `已批准 · v${version}` : `草稿 · v${version}`" /></header>
          <p class="prompt-text">{{ form.prompt }}</p>
          <footer><button class="ui-button" :disabled="busy" @click="emit('refine')"><RefreshCw :size="12" :stroke-width="2" />重新生成</button><button class="ui-button primary" :disabled="busy || approved" @click="emit('approve')"><Check :size="12" :stroke-width="2.5" />{{ approved ? '已批准' : '批准 Prompt' }}</button></footer>
        </div>
      </div>

      <div v-if="showPlan" class="msg-row assistant">
        <span class="msg-avatar"><img src="/assets/kantoku-avatar.png" alt="监督酱" /></span>
        <div class="msg-card plan-card">
          <header><strong>生成方案确认</strong><span class="plan-note"><ShieldCheck :size="12" :stroke-width="2" />预算保护已生效</span></header>
          <dl><div v-for="row in planRows" :key="row.label"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></div></dl>
          <footer><button class="ui-button primary full" :disabled="busy" @click="emit('generate')"><Zap :size="13" :stroke-width="2" />确认并开始生成 · {{ estimate }}</button></footer>
        </div>
      </div>

      <div v-if="busy" class="msg-row assistant"><span class="msg-avatar"><img src="/assets/kantoku-avatar.png" alt="监督酱" /></span><div class="msg-typing"><Loader2 :size="13" class="spin" :stroke-width="2" />正在处理，请稍候…</div></div>

      <div v-if="showResult" class="msg-row assistant">
        <span class="msg-avatar"><img src="/assets/kantoku-avatar.png" alt="监督酱" /></span>
        <div class="msg-card">
          <header><strong>产物已生成</strong><StatusBadge :status="task?.qc ? (task.review ? (task.review.approved ? 'completed' : 'failed') : 'waiting') : 'pending'" :label="task?.qc ? (task.review ? (task.review.approved ? '审批通过' : '审批未过') : '等待审批') : '等待质检'" /></header>
          <div v-if="candidates.length > 1" class="candidate-grid" :data-count="Math.min(candidates.length, 4)">
            <button v-for="item in candidates" :key="item.task.request_id" type="button" class="candidate" :class="{ selected: item.task.request_id === task?.request_id }" :title="`候选 · ${item.task.created_at}`" @click="emit('openCandidate', item.task)">
              <img v-if="item.url" :src="item.url" :alt="`候选 ${item.task.request_id}`" />
              <span v-else class="candidate-loading">产物加载中…</span>
              <i class="candidate-check"><Check :size="11" :stroke-width="3" /></i>
            </button>
          </div>
          <div v-else class="chat-artifact"><img v-if="artworkUrl" :src="artworkUrl" alt="当前产物" /><span v-else>产物加载中…</span></div>
          <p v-if="candidates.length > 1" class="candidate-hint">同一镜号共 {{ candidates.length }} 个候选，当前选中第 {{ candidates.findIndex((item) => item.task.request_id === task?.request_id) + 1 }} 个</p>
          <p v-if="task?.qc" class="chat-qc">质检：{{ task.qc.reason }}</p>
          <footer>
            <button class="ui-button primary" :disabled="busy" @click="emit('generate')"><Zap :size="13" :stroke-width="2" />再次生成 · {{ estimate }}</button>
            <button class="ui-button" :disabled="busy" @click="emit('refine')"><RefreshCw :size="12" :stroke-width="2" />重新整理 Prompt</button>
            <button class="ui-button quiet" @click="emit('recover')"><Search :size="12" :stroke-width="2" />查询原任务</button>
            <button class="ui-button quiet" @click="emit('resume')"><RotateCcw :size="12" :stroke-width="2" />恢复任务</button>
          </footer>
        </div>
      </div>
    </div>

    <footer class="chat-input">
      <textarea v-model="form.subject" rows="2" placeholder="描述主体、动作、场景、目标受众和交付用途…" @keydown.ctrl.enter="emit('compose')"></textarea>
      <button class="ui-button primary" :disabled="busy || !hasSubject" @click="emit('compose')"><Send :size="13" :stroke-width="2" />{{ hasPrompt ? '重新整理' : '生成 Prompt' }}<kbd>Ctrl ↵</kbd></button>
    </footer>
  </section>
</template>
