<script setup lang="ts">
import { computed } from 'vue'
import { directorPageFields, directorPageFieldLabel, directorStageSections, publicDirectorSections } from '../domains/comic/directorPresentation'
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
  return { stage, title: ({ creative_understanding: '创作核心', visual_direction: '视觉策略', cinematography: '摄影方案' } as Record<string, string>)[stage]!,
    fields: Object.values(directorPageFields(directorStageSections[stage]!, section.fields, props.mode)).slice(0, 2) }
}))
const review = computed(() => props.critic ?? props.spec?.critic_result as typeof props.critic)
function display(value: unknown): string { return Array.isArray(value) ? value.join('；') : typeof value === 'string' ? value : '' }
</script>
<template>
  <div class="director-node-view">
    <div class="node-actions">
      <button v-if="editable && !editing && stage !== 'director_assemble'" class="ui-button sm" :disabled="busy" @click="emit('edit')">{{ mode === 'fast' ? '修改' : '编辑节点' }}</button>
      <button v-if="rerunnable" class="ui-button quiet sm" :disabled="busy || editing" @click="emit('rerun')">从此节点重新执行</button>
    </div>
    <p v-if="node?.output_summary && !spec && mode !== 'fast'">{{ node.output_summary }}</p>
    <template v-if="editing">
      <label v-for="(value, key) in editFields" :key="key" class="node-edit-field">{{ directorPageFieldLabel(sectionKey, String(key).split('.').at(-1)!, mode) }}
        <textarea :value="value" rows="3" @input="emit('field', String(key), ($event.target as HTMLTextAreaElement).value)" />
      </label>
      <p class="pane-note">硬约束不可在这里修改。保存会将各节点待保存修改一起追加为新版本；审核通过并确认后才能继续。</p>
      <button class="ui-button primary sm" :disabled="busy" @click="emit('save')">保存草稿版本</button>
      <button class="ui-button quiet sm" :disabled="busy" @click="emit('cancel')">放弃本节点修改</button>
    </template>
    <template v-else-if="stage === 'director_assemble'">
      <div class="plan-overview">
        <article v-for="card in overview" :key="card.stage" class="plan-card">
          <header><h3>{{ card.title }}</h3><button class="ui-button quiet sm" @click="emit('open', card.stage)">查看 / 编辑</button></header>
          <p v-for="(value, index) in card.fields" :key="index">{{ display(value) }}</p>
          <p v-if="!card.fields.length" class="pane-note">本版本尚未提供此部分内容。</p>
        </article>
      </div>
      <p v-if="!overview.length" class="pane-note">先在同一个导演对话中描述创意，生成方案后可在这里查看与确认。</p>
    </template>
    <template v-else-if="stage !== 'director_critic'">
      <dl><div v-for="(value, key) in pageFields" :key="key"><dt>{{ directorPageFieldLabel(sectionKey, key, mode) }}</dt><dd>{{ display(value) }}</dd></div></dl>
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
h3 { font-size:15px; margin:22px 0 12px; } dl { margin:0; } dl > div { margin:16px 0; } dt { font-weight:600; } dd { margin:4px 0 0; white-space:pre-wrap; }
.plan-overview { display:grid; gap:12px; }
.plan-card { border:1px solid var(--border); border-radius:var(--radius-control); padding:14px 18px; }
.plan-card header { display:flex; gap:12px; justify-content:space-between; align-items:center; }
.plan-card h3 { margin:0; } .plan-card p { margin:10px 0 0; white-space:pre-wrap; }
.node-edit-field { display:grid; gap:7px; margin:16px 0; }
textarea { width:100%; box-sizing:border-box; resize:vertical; padding:10px; border:1px solid var(--border); border-radius:var(--radius-control); color:var(--text-primary); background:var(--surface); font:inherit; }
.critic-finding { border-left:2px solid var(--border); padding-left:14px; margin:18px 0; }
textarea:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
</style>
