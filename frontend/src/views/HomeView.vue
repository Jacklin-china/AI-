<script setup lang="ts">
import { ArrowRight, Sparkles, Wand2 } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { domains, type DomainDefinition } from '../domains'
import type { StudioTask } from '../types'
import EmptyState from '../components/EmptyState.vue'

const props = defineProps<{ tasks: StudioTask[]; busy: boolean }>()
const emit = defineEmits<{ navigate: [path: string]; quickStart: [domain: DomainDefinition]; openTask: [task: StudioTask, path: string]; create: [requirement: string] }>()

/* ---- 打字机问候 ---- */
const greetingFull = '你好，我是监督酱'
const greeting = ref('')
let typeTimer: number | undefined
function typeGreeting(): void {
  let index = 0
  typeTimer = window.setInterval(() => {
    index += 1
    greeting.value = greetingFull.slice(0, index)
    if (index >= greetingFull.length && typeTimer !== undefined) window.clearInterval(typeTimer)
  }, 110)
}

/* ---- 需求输入 ---- */
const requirement = ref('')
const focused = ref(false)
const suggestions = [
  { label: '商品主图', text: '为一款不锈钢保温杯生成白底电商主图，突出材质与刻度细节' },
  { label: '角色立绘', text: '生成一位银发女性角色的半身立绘，幻想风格，干净背景' },
  { label: '广告横幅', text: '生成一张极简风格的咖啡品牌广告横幅，暖色调，留出文案区' },
  { label: '场景概念', text: '生成雨后城市街道的场景概念图，电影感光线，写实风格' },
]
const placeholderIndex = ref(0)
const placeholder = computed(() => (focused.value || requirement.value ? '描述你的创作需求…' : `试试：${suggestions[placeholderIndex.value].text}`))
let rotationTimer: number | undefined
function submit(): void { const text = requirement.value.trim(); if (text) emit('create', text) }
function applyTemplate(text: string): void { requirement.value = text }

/* ---- 灵感模板流：点「做同款」把提示词带进工作台，仍需人工确认后才付费生成 ---- */
const templates: { tag: string; title: string; prompt: string; tone: string }[] = [
  { tag: '电商', title: '白底商品主图', prompt: '为一款不锈钢保温杯生成白底电商主图，突出金属材质与容量刻度，柔和顶光，干净背景', tone: 'blue' },
  { tag: '角色', title: '角色半身立绘', prompt: '生成一位银发女性角色的半身立绘，幻想风格，服装有东方纹样，干净背景，柔和边缘光', tone: 'violet' },
  { tag: '漫剧', title: '雨夜街头分镜', prompt: '雨后城市街道的场景概念图，霓虹反射在积水里，电影感光线，写实风格，16:9', tone: 'teal' },
  { tag: '海报', title: '极简咖啡海报', prompt: '生成一张极简风格的咖啡品牌广告横幅，暖色调，右侧留出文案区，留出品牌位', tone: 'amber' },
  { tag: '概念', title: '古风庭院空镜', prompt: '生成一座清晨薄雾中的古风庭院空镜，青瓦白墙，逆光，水墨质感，横构图', tone: 'green' },
  { tag: '分镜', title: '对峙特写镜头', prompt: '两位角色在狭窄走廊对峙的特写分镜，低角度，冷暖光对比，戏剧化构图', tone: 'rose' },
]
function useTemplate(prompt: string): void { emit('create', prompt) }

/* ---- 生产流水线 ---- */
type StageId = 'plan' | 'prompt' | 'generate' | 'qc' | 'review' | 'delivery'
const stages: { id: StageId; name: string; hint: string }[] = [
  { id: 'plan', name: '需求规划', hint: '明确交付目标' },
  { id: 'prompt', name: 'Prompt', hint: '构建与批准' },
  { id: 'generate', name: '生成', hint: '预算保护执行' },
  { id: 'qc', name: '质检', hint: 'VLM 预筛' },
  { id: 'review', name: '审批', hint: '人工终审' },
  { id: 'delivery', name: '交付', hint: '归档与溯源' },
]
function stageOf(task: StudioTask): StageId {
  if (task.archived || task.review) return 'delivery'
  if (task.qc) return 'review'
  if (task.has_image) return 'qc'
  if (task.prompt) return 'generate'
  return 'plan'
}
const pipeline = computed(() => stages.map((stage) => ({ ...stage, tasks: props.tasks.filter((task) => stageOf(task) === stage.id) })))
const activeStageIds = computed(() => new Set(pipeline.value.filter((stage) => stage.tasks.length).map((stage) => stage.id)))
function money(task: StudioTask): string { return `¥${((task.actual_fen ?? task.estimate_fen) / 100).toFixed(2)}` }
function time(raw: string): string { if (!raw) return '—'; const value = new Date(raw.replace(' ', 'T') + 'Z'); return Number.isNaN(value.valueOf()) ? raw : value.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) }
function tip(task: StudioTask): string { return `${time(task.created_at)} · ${money(task)}${task.prompt ? `\n${task.prompt.slice(0, 60)}` : ''}` }

/* ---- 右栏 ---- */
const waiting = computed(() => props.tasks.filter((task) => task.status === 'unknown' || (task.qc && !task.review) || task.rework?.status === 'pending'))
const displayStats = ref({ running: 0, waiting: 0, total: 0 })
function tween(key: keyof typeof displayStats.value, target: number): void {
  const from = displayStats.value[key]
  if (from === target) return
  const start = performance.now()
  const step = (now: number): void => {
    const t = Math.min(1, (now - start) / 420)
    displayStats.value[key] = Math.round(from + (target - from) * (1 - (1 - t) ** 3))
    if (t < 1) requestAnimationFrame(step)
  }
  requestAnimationFrame(step)
}
function syncStats(): void { tween('running', props.busy ? 1 : 0); tween('waiting', waiting.value.length); tween('total', props.tasks.length) }
function domainAction(domain: (typeof domains)[number]): string { return domain.status === 'available' ? '进入创作' : domain.status === 'preview' ? '查看定义' : '待上线' }

onMounted(() => { typeGreeting(); rotationTimer = window.setInterval(() => { placeholderIndex.value = (placeholderIndex.value + 1) % suggestions.length }, 3600); syncStats() })
watch([() => props.busy, waiting, () => props.tasks.length], syncStats)
onBeforeUnmount(() => { if (typeTimer !== undefined) window.clearInterval(typeTimer); if (rotationTimer !== undefined) window.clearInterval(rotationTimer) })
</script>

<template>
  <main class="page home-page">
    <div class="home-main">
      <section class="assistant-bar">
        <span class="assistant-avatar"><img src="/assets/kantoku-avatar.png" alt="监督酱" /></span>
        <div class="assistant-main">
          <header class="assistant-head"><h1>{{ greeting }}<i class="type-caret"></i></h1><p>让 AI 创作可控、可追溯、可规模化。描述需求，我会规划、估价并在你确认后执行。</p></header>
          <div class="chip-row function-rail">
            <button v-for="domain in domains" :key="domain.id" type="button" class="chip" :disabled="domain.status === 'coming_soon'" :title="domainAction(domain)" @click="emit('quickStart', domain)">
              <span class="chip-mark">{{ domain.icon }}</span>{{ domain.label }}
            </button>
          </div>

          <div class="requirement-box">
            <textarea v-model="requirement" rows="3" :placeholder="placeholder" @focus="focused = true" @blur="focused = false" @keydown.ctrl.enter="submit" @keydown.meta.enter="submit"></textarea>
            <footer>
              <div class="chip-row"><button v-for="item in suggestions" :key="item.label" type="button" class="chip" @click="applyTemplate(item.text)"><Wand2 :size="11" :stroke-width="2" />{{ item.label }}</button></div>
              <button class="ui-button primary lg" :disabled="!requirement.trim()" @click="submit"><Sparkles :size="14" :stroke-width="2" />开始创作<kbd>Ctrl ↵</kbd></button>
            </footer>
          </div>
        </div>
      </section>

      <section class="inspiration-section">
        <header class="section-row">
          <div><span class="section-kicker">灵感</span><h2>从模板开始，改一点点就好</h2></div>
          <span class="count-chip">{{ templates.length }} 个模板</span>
        </header>
        <div class="inspiration-grid">
          <button v-for="item in templates" :key="item.title" type="button" class="inspiration-card" @click="useTemplate(item.prompt)">
            <span class="inspiration-mark" :data-tone="item.tone">{{ item.tag }}</span>
            <strong>{{ item.title }}</strong>
            <span class="inspiration-desc">{{ item.prompt }}</span>
            <span class="inspiration-foot">做同款<ArrowRight :size="12" :stroke-width="2" /></span>
          </button>
        </div>
      </section>
    </div>

    <aside class="home-rail">
      <section class="rail-stats">
        <div><b class="num">{{ displayStats.running }}</b><span>运行中</span></div>
        <div><b class="num">{{ displayStats.waiting }}</b><span>待处理</span></div>
        <div><b class="num">{{ displayStats.total }}</b><span>总任务</span></div>
      </section>
      <section class="pipeline-rail">
        <header class="section-row"><div><span class="section-kicker">产线</span><h2>生产流水线</h2></div><span>{{ tasks.length }} 个镜头</span></header>
        <ol class="pipeline-vertical">
          <li v-for="stage in pipeline" :key="stage.id" :class="{ active: activeStageIds.has(stage.id) }">
            <div class="pv-rail"><i>{{ stage.tasks.length }}</i><span></span></div>
            <div class="pv-body">
              <div class="stage-title"><strong>{{ stage.name }}</strong><small>{{ stage.hint }}</small></div>
              <div v-if="stage.tasks.length" class="pv-chips">
                <button v-for="task in stage.tasks.slice(0, 2)" :key="task.request_id" class="shot-chip" :class="{ failed: task.status === 'failed' }" :data-tip="tip(task)" @click="emit('openTask', task, `/runs/${encodeURIComponent(task.request_id)}`)"><strong>{{ task.project }}</strong><small>#{{ task.shot_no }}</small></button>
                <span v-if="stage.tasks.length > 2" class="stage-more">+{{ stage.tasks.length - 2 }}</span>
              </div>
            </div>
          </li>
        </ol>
      </section>
      <section class="attention-panel">
        <header class="section-row"><div><span class="section-kicker">待办</span><h2>需要处理</h2></div><span>{{ waiting.length }}</span></header>
        <div v-if="waiting.length" class="attention-list">
          <button v-for="task in waiting.slice(0, 5)" :key="task.request_id" @click="emit('openTask', task, `/runs/${encodeURIComponent(task.request_id)}`)"><i></i><div><strong>{{ task.project }}</strong><small>{{ task.qc && !task.review ? '等待人工审批' : task.status === 'unknown' ? '账单或任务状态待核对' : '返修项待处理' }}</small></div><ArrowRight :size="12" /></button>
        </div>
        <EmptyState v-else title="没有待处理事项" description="审批、失败和未知账单会出现在这里。" icon="✓" />
      </section>
    </aside>
  </main>
</template>
