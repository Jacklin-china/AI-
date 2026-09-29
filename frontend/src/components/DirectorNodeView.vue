<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRight } from 'lucide-vue-next'
import { directorPageFields, directorPageFieldLabel, directorStageSections, fastDirectorNodeLabels, directorNodeLabels, publicDirectorSections } from '../domains/comic/directorPresentation'
import type { DirectorNodeSummary } from '../services/core'
const props = defineProps<{
  stage: string; node?: DirectorNodeSummary; spec: Record<string, unknown> | null; editing: boolean
  fields: Record<string, string>; editable: boolean; rerunnable: boolean; busy: boolean
  mode?: 'fast' | 'professional'
  critic?: { verdict: string; public_summary: string; findings: { code: string; evidence: string; suggested_action: string }[] } | null
}>()
const emit = defineEmits<{ edit: []; rerun: []; save: []; cancel: []; field: [key: string, value: string]; revise: []; open: [stage: string] }>()
const sectionKey = computed(() => directorStageSections[props.stage] ?? '')
const body = computed(() => {
  // Saved v2 is the current editable version. Never overlay it with an older Run's output.
  const saved = props.spec?.[sectionKey.value]
  return (saved ?? (props.node ? Object.values(props.node.output)[0] : {})) as Record<string, unknown> ?? {}
})
const pageFields = computed(() => directorPageFields(sectionKey.value, body.value, props.mode))
const editFields = computed(() => Object.fromEntries(Object.entries(props.fields).filter(([path]) => {
  const [section, field] = path.includes('.') ? path.split('.') : [sectionKey.value, path]
  return section === sectionKey.value && field !== 'hard_constraints' && Object.keys(directorPageFields(section!, { [field!]: '' }, props.mode)).length > 0
})))
const overview = computed(() => publicDirectorSections(props.spec).map(section => {
  const stage = ({ 创作理解: 'creative_understanding', 导演方案: 'visual_direction', 摄影方案: 'cinematography' } as Record<string, string>)[section.title]!
  return { stage, title: (props.mode === 'fast' ? fastDirectorNodeLabels : directorNodeLabels)[stage]!,
    fields: Object.values(directorPageFields(directorStageSections[stage]!, section.fields, props.mode)).slice(0, 1) }
}))
const review = computed(() => props.critic ?? props.spec?.critic_result as typeof props.critic)
function display(value: unknown): string { return Array.isArray(value) ? value.join('；') : typeof value === 'string' ? value : '' }
</script>
<template>
  <div class="director-node-view">
    <div class="node-actions">
      <button v-if="editable && !editing && stage !== 'director_assemble'" class="ui-button sm" :disabled="busy" @click="emit('edit')">{{ mode === 'fast' ? '修改' : '编辑节点' }}</button>
      <button v-if="rerunnable && stage !== 'director_assemble'" class="ui-button quiet sm" :disabled="busy || editing" @click="emit('rerun')">从此节点重新执行</button>
    </div>
    <p v-if="node?.output_summary && !spec && mode !== 'fast'">{{ node.output_summary }}</p>
    <template v-if="editing">
      <div class="node-fields editor-fields"><label v-for="(value, key) in editFields" :key="key" class="node-edit-field">{{ directorPageFieldLabel(sectionKey, String(key).split('.').at(-1)!, mode) }}
        <textarea :value="value" rows="3" @input="emit('field', String(key), ($event.target as HTMLTextAreaElement).value)" />
      </label></div>
      <p class="pane-note">硬约束不可在这里修改。保存会将各节点待保存修改一起追加为新版本；审核通过并确认后才能继续。</p>
      <button class="ui-button primary sm" :disabled="busy" @click="emit('save')">保存草稿版本</button>
      <button class="ui-button quiet sm" :disabled="busy" @click="emit('cancel')">放弃本节点修改</button>
    </template>
    <template v-else-if="stage === 'director_assemble'">
      <p v-if="overview.length" class="plan-invitation">{{ mode === 'fast' ? '逐项查看建议，调整成你想要的方向。' : '检查各节点的公开决策，再确认这一版导演稿。' }}</p>
      <nav class="plan-index" aria-label="方案组成">
        <button v-for="part in overview" :key="part.stage" @click="emit('open', part.stage)"><strong>{{ part.title }}</strong><span>{{ display(part.fields[0]) || '尚未提供此部分内容' }}</span><ArrowRight :size="16" /></button>
      </nav>
      <p v-if="!overview.length" class="pane-note">先在同一个导演对话中描述创意，生成方案后可在这里查看与确认。</p>
    </template>
    <template v-else-if="stage !== 'director_critic'">
      <dl class="node-fields" :class="{ approachable: mode === 'fast' }"><div v-for="(value, key) in pageFields" :key="key"><dt>{{ directorPageFieldLabel(sectionKey, key, mode) }}</dt><dd>{{ display(value) }}</dd></div></dl>
      <p v-if="!Object.keys(pageFields).length" class="pane-note">此节点尚无公开结果。先在对话中描述创意，或查看当前任务状态。</p>
    </template>
    <section v-if="stage === 'director_critic' && mode !== 'fast'">
      <template v-if="review"><h3>{{ review.verdict === 'pass' ? '审核通过' : review.verdict === 'blocked' ? '审核阻止继续' : '需要调整' }}</h3><p>{{ review.public_summary }}</p>
        <article v-for="finding in review.findings" :key="finding.code + finding.evidence" class="critic-finding"><strong>{{ finding.code }}</strong><p>{{ finding.evidence }}</p><p>{{ finding.suggested_action }}</p></article>
        <button v-if="review.verdict !== 'pass'" class="ui-button sm" @click="emit('revise')">前往视觉导演修改</button>
        <p class="pane-note">建议仅供参考；界面不会将建议冒充已应用的修订。</p>
      </template><p v-else class="pane-note">暂无真实审核结果。</p>
    </section>
  </div>
</template>
<style scoped>
.node-actions { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }
h3 { font-size:15px; margin:22px 0 12px; }
.node-fields { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:24px 32px; margin:0; }
.node-fields > div { min-width:0; }
dt { font-size:12px; color:var(--text-secondary); font-weight:500; margin-bottom:8px; }
dd { margin:0; white-space:pre-wrap; overflow-wrap:anywhere; }
.approachable { grid-template-columns:1fr; max-width:640px; gap:24px; }
.approachable dt { font-size:13px; }
.plan-invitation { margin:0 0 24px; color:var(--text-secondary); }
.plan-index { border-top:1px solid var(--border); }
.plan-index button { display:grid; grid-template-columns:90px minmax(0,1fr) 16px; gap:20px; align-items:center; text-align:left; width:100%; border:0; border-bottom:1px solid var(--border); background:transparent; color:var(--text-primary); font:inherit; padding:20px 0; cursor:pointer; }
.plan-index button:hover { color:var(--accent); }
.plan-index strong { font-size:13px; } .plan-index span { color:var(--text-secondary); display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.node-edit-field { display:grid; gap:8px; margin:0; font-size:12px; color:var(--text-secondary); }
textarea { width:100%; box-sizing:border-box; resize:vertical; padding:10px; border:1px solid var(--border); border-radius:var(--radius-control); color:var(--text-primary); background:var(--surface); font:inherit; }
.critic-finding { border-left:2px solid var(--border); padding-left:14px; margin:18px 0; }
textarea:focus-visible, button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
@media(max-width:800px) { .node-fields { grid-template-columns:1fr; } .plan-index button { grid-template-columns:minmax(0,1fr) 16px; gap:8px; } .plan-index strong { grid-column:1; } .plan-index span { grid-column:1; grid-row:2; } .plan-index svg { grid-column:2; grid-row:1/3; } }
</style>
