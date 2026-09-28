<script setup lang="ts">
import { computed } from 'vue'
import { directorFieldLabels, publicDirectorSections } from '../domains/comic/directorPresentation'
import type { DirectorNodeSummary } from '../services/core'
const props = defineProps<{
  stage: string; node?: DirectorNodeSummary; spec: Record<string, unknown> | null; editing: boolean
  fields: Record<string, string>; editable: boolean; rerunnable: boolean; busy: boolean
  critic?: { verdict: string; public_summary: string; findings: { code: string; evidence: string; suggested_action: string }[] } | null
}>()
const emit = defineEmits<{ edit: []; rerun: []; save: []; cancel: []; field: [key: string, value: string]; revise: [] }>()
const sections = computed(() => props.node && props.stage !== 'director_assemble'
  ? [{ title: '', fields: Object.fromEntries(Object.entries(Object.values(props.node.output)[0] ?? {}).filter(([key, value]) => key in directorFieldLabels && value != null)) }]
  : props.stage === 'director_assemble' ? publicDirectorSections(props.spec)
    : publicDirectorSections(props.spec).filter(section => section.title === ({ creative_understanding: '创作理解', visual_direction: '导演方案', cinematography: '摄影方案' } as Record<string, string>)[props.stage]))
const review = computed(() => props.critic ?? props.spec?.critic_result as typeof props.critic)
function display(value: unknown): string { return Array.isArray(value) ? value.join('；') : typeof value === 'string' ? value : '' }
</script>
<template>
  <div class="director-node-view">
    <div class="node-actions">
      <button v-if="editable && !editing" class="ui-button sm" :disabled="busy" @click="emit('edit')">修改方案</button>
      <button v-if="rerunnable" class="ui-button quiet sm" :disabled="busy || editing" @click="emit('rerun')">从此节点重新执行</button>
    </div>
    <p v-if="node?.output_summary">{{ node.output_summary }}</p>
    <template v-if="editing">
      <label v-for="(value, key) in fields" :key="key" class="node-edit-field">{{ directorFieldLabels[key] ?? key }}
        <textarea :value="value" rows="3" @input="emit('field', String(key), ($event.target as HTMLTextAreaElement).value)" />
      </label>
      <p class="pane-note">硬约束不可在这里修改。保存会追加方案版本，并重新执行后续审核。</p>
      <button class="ui-button primary sm" :disabled="busy" @click="emit('save')">保存并重新审核</button>
      <button class="ui-button quiet sm" :disabled="busy" @click="emit('cancel')">放弃本节点修改</button>
    </template>
    <template v-else>
      <section v-for="section in sections" :key="section.title"><h3 v-if="section.title">{{ section.title }}</h3>
        <dl><div v-for="(value, key) in section.fields" :key="key"><dt>{{ directorFieldLabels[key] ?? key }}</dt><dd>{{ display(value) }}</dd></div></dl>
      </section>
      <p v-if="!sections.some(section => Object.keys(section.fields).length) && stage !== 'director_critic'" class="pane-note">此节点尚无完成结果。先在对话中描述创意，或查看当前任务状态。</p>
    </template>
    <section v-if="stage === 'director_critic'">
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
.node-edit-field { display:grid; gap:7px; margin:16px 0; }
textarea { width:100%; box-sizing:border-box; resize:vertical; padding:10px; border:1px solid var(--border); border-radius:var(--radius-control); color:var(--text-primary); background:var(--surface); font:inherit; }
.critic-finding { border-left:2px solid var(--border); padding-left:14px; margin:18px 0; }
textarea:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
</style>
