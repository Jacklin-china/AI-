<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ArrowRight, Check, Copy, LocateFixed, Pencil } from 'lucide-vue-next'
import { directorFeedbackTarget, directorPageFields, directorPageFieldLabel, directorStageSections, fastDirectorNodeLabels, directorNodeLabels, publicDirectorSections } from '../domains/comic/directorPresentation'
import type { CriticFeedback, DirectorNodeSummary } from '../services/core'
const props = defineProps<{
  stage: string; node?: DirectorNodeSummary; spec: Record<string, unknown> | null; editing: boolean
  fields: Record<string, string>; editable: boolean; rerunnable: boolean; busy: boolean
  mode?: 'fast' | 'professional'
  criticFeedback?: CriticFeedback[]
  critic?: { verdict: string; public_summary: string; findings: { code: string; evidence: string; suggested_action: string }[] } | null
}>()
const emit = defineEmits<{ edit: []; rerun: []; save: []; cancel: []; field: [key: string, value: string]; revise: []; open: [stage: string]; locate: [path: string] }>()
const root = ref<HTMLElement | null>(null)
const focusedField = ref('')
const copiedSuggestion = ref('')
const copyError = ref('')
watch(() => [props.stage, props.editing, props.spec?.version], () => {
  focusedField.value = ''; copiedSuggestion.value = ''; copyError.value = ''
})
const sectionKey = computed(() => directorStageSections[props.stage] ?? '')
const body = computed(() => {
  // Saved v2 is the current editable version. Never overlay it with an older Run's output.
  const saved = props.spec?.[sectionKey.value]
  return (saved ?? (props.node ? Object.values(props.node.output)[0] : {})) as Record<string, unknown> ?? {}
})
const pageFields = computed(() => directorPageFields(sectionKey.value, body.value, props.mode))
const editFields = computed(() => Object.fromEntries(Object.entries(props.fields).filter(([path]) => {
  const [section, field] = path.includes('.') ? path.split('.') : [sectionKey.value, path]
  return section === sectionKey.value && field !== 'hard_constraints' && (path === focusedField.value || Object.keys(directorPageFields(section!, { [field!]: '' }, props.mode)).length > 0)
})))
const overview = computed(() => publicDirectorSections(props.spec).map(section => {
  const stage = ({ 创作理解: 'creative_understanding', 导演方案: 'visual_direction', 摄影方案: 'cinematography' } as Record<string, string>)[section.title]!
  return { stage, title: (props.mode === 'fast' ? fastDirectorNodeLabels : directorNodeLabels)[stage]!,
    fields: Object.values(directorPageFields(directorStageSections[stage]!, section.fields, props.mode)).slice(0, 1) }
}))
const review = computed(() => props.spec && 'critic_result' in props.spec ? props.spec.critic_result as typeof props.critic : props.critic)
const feedback = computed(() => props.spec?.critic_feedback as CriticFeedback[] ?? props.criticFeedback ?? [])
const blocking = computed(() => feedback.value.some(item => item.level === 'blocking'))
function suggestionKey(finding: CriticFeedback): string { return `${finding.affected_field}:${finding.message}:${finding.suggestion}` }
async function copySuggestion(finding: CriticFeedback): Promise<void> {
  copiedSuggestion.value = ''; copyError.value = ''
  try {
    await navigator.clipboard.writeText(`${finding.affected_field_label}：${finding.suggestion}`)
    copiedSuggestion.value = suggestionKey(finding)
  } catch { copyError.value = '复制失败，请选中建议文字复制。' }
}
async function focusField(path: string): Promise<void> {
  if (!props.editing || !(path in props.fields) || !directorFeedbackTarget(props.spec, path)) return
  focusedField.value = path
  await nextTick()
  const input = root.value?.querySelector<HTMLTextAreaElement>(`textarea[data-director-field="${path}"]`)
  input?.focus({ preventScroll: true })
  input?.scrollIntoView({ block: 'center', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' })
}
defineExpose({ focusField })
function display(value: unknown): string { return Array.isArray(value) ? value.join('；') : typeof value === 'string' ? value : '' }
</script>
<template>
  <div ref="root" class="director-node-view">
    <div class="node-actions">
      <button v-if="editable && !editing && stage !== 'director_assemble'" class="ui-button sm" :disabled="busy" @click="emit('edit')">
        <Pencil :size="14" /> {{ mode === 'fast' ? '修改' : '编辑节点' }}
      </button>
      <button v-if="rerunnable && stage !== 'director_assemble'" class="ui-button quiet sm" :disabled="busy || editing" @click="emit('rerun')">从此节点重新执行</button>
    </div>
    <p v-if="node?.output_summary && !spec && mode !== 'fast'" class="pane-note">{{ node.output_summary }}</p>

    <!-- 编辑模式：单列卡片式编辑 -->
    <template v-if="editing">
      <div class="field-cards editor-cards">
        <div v-for="(value, key) in editFields" :key="key" class="field-card">
          <label :for="`director-${key}`" class="field-card-label">{{ directorPageFieldLabel(sectionKey, String(key).split('.').at(-1)!, mode) }}</label>
          <textarea :id="`director-${key}`" :data-director-field="key" :class="{ 'located-field': focusedField === key }" :value="value" rows="3" @input="emit('field', String(key), ($event.target as HTMLTextAreaElement).value)" />
        </div>
      </div>
      <p class="pane-note">硬约束不可在这里修改。保存会将各节点待保存修改一起追加为新版本；审核通过并确认后才能继续。</p>
      <div class="editor-actions">
        <button class="ui-button primary sm" :disabled="busy" @click="emit('save')">保存修改</button>
        <button class="ui-button quiet sm" :disabled="busy" @click="emit('cancel')">取消</button>
      </div>
    </template>

    <!-- 最终方案汇总页：纵向导航卡片 -->
    <template v-else-if="stage === 'director_assemble'">
      <p v-if="overview.length" class="plan-invitation">{{ mode === 'fast' ? '逐项查看建议，调整成你想要的方向。' : '检查各节点的公开决策，再确认这一版导演稿。' }}</p>
      <nav class="plan-index" aria-label="方案组成">
        <button v-for="part in overview" :key="part.stage" class="plan-entry" @click="emit('open', part.stage)">
          <div class="plan-entry-header">
            <strong>{{ part.title }}</strong>
            <ArrowRight :size="16" class="plan-entry-arrow" />
          </div>
          <p class="plan-entry-preview">{{ display(part.fields[0]) || '尚未提供此部分内容' }}</p>
        </button>
      </nav>
      <p v-if="!overview.length" class="pane-note">先在同一个导演对话中描述创意，生成方案后可在这里查看与确认。</p>
    </template>

    <!-- 普通节点查看模式：单列卡片 -->
    <template v-else-if="stage !== 'director_critic'">
      <div class="field-cards">
        <div v-for="(value, key) in pageFields" :key="key" class="field-card">
          <dt class="field-card-label">{{ directorPageFieldLabel(sectionKey, key, mode) }}</dt>
          <dd class="field-card-content">{{ display(value) }}</dd>
        </div>
      </div>
      <p v-if="!Object.keys(pageFields).length" class="pane-note">此节点尚无公开结果。先在对话中描述创意，或查看当前任务状态。</p>
    </template>

    <!-- 审核节点 -->
    <p v-if="stage === 'director_critic' && spec?.review_required === false && node?.status === 'completed'" class="pane-note">方案检查已完成</p>
    <section v-if="spec?.review_required !== false && ['director_critic', 'director_assemble'].includes(stage) && (mode !== 'fast' || (stage === 'director_assemble' && feedback.length))" class="critic-section">
      <template v-if="review">
        <h3 class="critic-title" :class="blocking ? 'blocked' : feedback.length ? 'needs-revision' : 'pass'">
          {{ blocking ? '需要修改后确认' : feedback.length ? '审核建议，可确认方案' : review.verdict === 'pass' ? '审核通过' : '审核说明待更新' }}
        </h3>
        <article v-for="finding in feedback" :key="finding.affected_field + finding.message" class="critic-finding">
          <strong class="critic-code">问题：{{ finding.message }}</strong>
          <p class="critic-evidence">影响：{{ finding.impact }}</p>
          <p class="critic-evidence">原因：{{ finding.reason }}</p>
          <p class="critic-suggestion">修改建议：{{ finding.suggestion }}</p>
          <div class="finding-actions">
            <button class="ui-button quiet sm" :title="copiedSuggestion === suggestionKey(finding) ? '已复制建议' : '复制建议'" :aria-label="copiedSuggestion === suggestionKey(finding) ? '已复制建议' : '复制建议'" @click="copySuggestion(finding)">
              <Check v-if="copiedSuggestion === suggestionKey(finding)" :size="15" /><Copy v-else :size="15" />
            </button>
            <button v-if="directorFeedbackTarget(spec, finding.affected_field)" class="ui-button quiet sm" :disabled="busy" :title="`定位修改：${finding.affected_field_label}`" :aria-label="`定位修改：${finding.affected_field_label}`" @click="emit('locate', finding.affected_field)">
              <LocateFixed :size="15" /><span>{{ finding.affected_field_label }}</span>
            </button>
          </div>
        </article>
        <p v-if="copyError" class="copy-error" role="alert">{{ copyError }}</p>
        <span v-if="copiedSuggestion" class="sr-only" role="status">已复制建议</span>
        <button v-if="feedback.length" class="ui-button sm" @click="emit('revise')">修改导演方案</button>
        <p v-if="review.findings?.length && !feedback.length" class="pane-note">此历史审核缺少问题说明，请重新审核后查看具体原因。</p>
        <p class="pane-note">建议仅供参考；界面不会将建议冒充已应用的修订。</p>
      </template>
      <p v-else class="pane-note">暂无真实审核结果。</p>
    </section>
  </div>
</template>

<style scoped>
.director-node-view { max-width:720px; margin:0 auto; }
.node-actions { display:flex; gap:var(--space-2); flex-wrap:wrap; margin-bottom:var(--space-6); }
.node-actions .ui-button { display:inline-flex; align-items:center; gap:var(--space-1); }

/* 连续节点正文：保留原有排版，不逐字段套卡片。 */
.field-cards { display:flex; flex-direction:column; gap:var(--space-5); }
.field-card {
  background:transparent;
  border:0;
  padding:0 0 var(--space-4);
}
.field-card-label {
  display:block;
  font-size: var(--font-meta);
  font-weight:500;
  color:var(--text-secondary);
  margin-bottom:var(--space-2);
  letter-spacing:0.01em;
}
.field-card-content {
  margin:0;
  font-size: var(--font-body);
  line-height:1.7;
  color:var(--text-primary);
  white-space:pre-wrap;
  overflow-wrap:anywhere;
}

/* 编辑模式 */
.editor-cards .field-card { padding:0 0 var(--space-4); }
.node-edit-field, .editor-cards .field-card { display:flex; flex-direction:column; gap:var(--space-2); }
.editor-cards label { font-size: var(--font-meta); color:var(--text-secondary); font-weight:500; }
textarea {
  width:100%;
  box-sizing:border-box;
  resize:vertical;
  padding:var(--space-3) var(--space-4);
  border:1px solid var(--border);
  border-radius:var(--radius-control);
  color:var(--text-primary);
  background:var(--canvas);
  font:inherit;
  font-size: var(--font-body);
  line-height:1.6;
  transition:border-color 120ms ease;
}
textarea:focus-visible { outline:none; border-color:var(--accent); }
textarea.located-field { border-color:var(--accent); outline:2px solid var(--accent); outline-offset:2px; }
.editor-actions { display:flex; gap:var(--space-2); margin-top:var(--space-5); }

/* 最终方案导航卡片 */
.plan-invitation { margin:0 0 var(--space-6); color:var(--text-secondary); font-size: var(--font-body); line-height:1.7; }
.plan-index { display:flex; flex-direction:column; gap:var(--space-3); }
.plan-entry {
  display:block;
  width:100%;
  text-align:left;
  border:0;
  border-bottom:1px solid var(--border-muted);
  border-radius:0;
  background:transparent;
  padding:var(--space-4) 0;
  cursor:pointer;
  transition:border-color 120ms ease, background 120ms ease;
}
.plan-entry:hover { background:var(--surface-subtle); }
.plan-entry-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:var(--space-2); }
.plan-entry-header strong { font-size: var(--font-body); font-weight:600; color:var(--text-primary); }
.plan-entry-arrow { color:var(--text-muted); flex-shrink:0; }
.plan-entry-preview {
  margin:0;
  font-size: var(--font-control);
  color:var(--text-secondary);
  line-height:1.6;
  display:-webkit-box;
  -webkit-line-clamp:2;
  -webkit-box-orient:vertical;
  overflow:hidden;
}

/* 审核区 */
.critic-section { margin-top:var(--space-2); }
.critic-title { font-size: var(--font-section); font-weight:600; margin:0 0 var(--space-3); }
.critic-title.pass { color:var(--success); }
.critic-title.blocked { color:var(--danger); }
.critic-title.needs-revision { color:var(--warning); }
.critic-summary { font-size: var(--font-body); line-height:1.7; color:var(--text-secondary); margin:0 0 var(--space-5); }
.critic-finding {
  border-left:3px solid var(--border-strong);
  padding:var(--space-3) var(--space-4);
  margin:var(--space-4) 0;
  background:var(--canvas);
  border-radius:0 var(--radius-control) var(--radius-control) 0;
}
.critic-code { font-size: var(--font-meta); font-weight:600; color:var(--text-secondary); }
.critic-evidence { margin:var(--space-2) 0 var(--space-1); font-size: var(--font-control); line-height:1.6; }
.critic-suggestion { margin:0; font-size: var(--font-meta); color:var(--text-secondary); }
.finding-actions { display:flex; align-items:center; gap:var(--space-2); margin-top:var(--space-2); }
.finding-actions .ui-button { display:inline-flex; align-items:center; gap:var(--space-1); min-height:var(--control-sm); }
.copy-error { color:var(--danger); font-size: var(--font-control); }
.sr-only { position:absolute; width:1px; height:1px; padding:0; overflow:hidden; clip-path:inset(50%); white-space:nowrap; }

button:focus-visible, textarea:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }

@media(max-width:600px) {
  .field-card { padding:0 0 var(--space-4); }
  .plan-entry { padding:var(--space-4) 0; }
}
</style>
