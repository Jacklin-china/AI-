<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRight, Pencil } from 'lucide-vue-next'
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
          <label class="field-card-label">{{ directorPageFieldLabel(sectionKey, String(key).split('.').at(-1)!, mode) }}</label>
          <textarea :value="value" rows="3" @input="emit('field', String(key), ($event.target as HTMLTextAreaElement).value)" />
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
    <section v-if="stage === 'director_critic' && mode !== 'fast'" class="critic-section">
      <template v-if="review">
        <h3 class="critic-title" :class="review.verdict === 'pass' ? 'pass' : review.verdict === 'blocked' ? 'blocked' : 'needs-revision'">
          {{ review.verdict === 'pass' ? '审核通过' : review.verdict === 'blocked' ? '审核阻止继续' : '需要调整' }}
        </h3>
        <p class="critic-summary">{{ review.public_summary }}</p>
        <article v-for="finding in review.findings" :key="finding.code + finding.evidence" class="critic-finding">
          <strong class="critic-code">{{ finding.code }}</strong>
          <p class="critic-evidence">{{ finding.evidence }}</p>
          <p class="critic-suggestion">{{ finding.suggested_action }}</p>
        </article>
        <button v-if="review.verdict !== 'pass'" class="ui-button sm" @click="emit('revise')">前往视觉导演修改</button>
        <p class="pane-note">建议仅供参考；界面不会将建议冒充已应用的修订。</p>
      </template>
      <p v-else class="pane-note">暂无真实审核结果。</p>
    </section>
  </div>
</template>

<style scoped>
.director-node-view { max-width:720px; margin:0 auto; }
.node-actions { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:24px; }
.node-actions .ui-button { display:inline-flex; align-items:center; gap:5px; }

/* 连续节点正文：保留原有排版，不逐字段套卡片。 */
.field-cards { display:flex; flex-direction:column; gap:20px; }
.field-card {
  background:transparent;
  border:0;
  padding:0 0 16px;
}
.field-card-label {
  display:block;
  font-size:12px;
  font-weight:500;
  color:var(--text-secondary);
  margin-bottom:10px;
  letter-spacing:0.01em;
}
.field-card-content {
  margin:0;
  font-size:14px;
  line-height:1.7;
  color:var(--text-primary);
  white-space:pre-wrap;
  overflow-wrap:anywhere;
}

/* 编辑模式 */
.editor-cards .field-card { padding:0 0 16px; }
.node-edit-field, .editor-cards .field-card { display:flex; flex-direction:column; gap:8px; }
.editor-cards label { font-size:12px; color:var(--text-secondary); font-weight:500; }
textarea {
  width:100%;
  box-sizing:border-box;
  resize:vertical;
  padding:12px 14px;
  border:1px solid var(--border);
  border-radius:8px;
  color:var(--text-primary);
  background:var(--canvas);
  font:inherit;
  font-size:14px;
  line-height:1.6;
  transition:border-color 120ms ease;
}
textarea:focus-visible { outline:none; border-color:var(--accent); }
.editor-actions { display:flex; gap:10px; margin-top:20px; }

/* 最终方案导航卡片 */
.plan-invitation { margin:0 0 24px; color:var(--text-secondary); font-size:14px; line-height:1.7; }
.plan-index { display:flex; flex-direction:column; gap:12px; }
.plan-entry {
  display:block;
  width:100%;
  text-align:left;
  border:0;
  border-bottom:1px solid var(--border-muted);
  border-radius:0;
  background:transparent;
  padding:16px 0;
  cursor:pointer;
  transition:border-color 120ms ease, background 120ms ease;
}
.plan-entry:hover { background:var(--surface-subtle); }
.plan-entry-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
.plan-entry-header strong { font-size:14px; font-weight:600; color:var(--text-primary); }
.plan-entry-arrow { color:var(--text-muted); flex-shrink:0; }
.plan-entry-preview {
  margin:0;
  font-size:13px;
  color:var(--text-secondary);
  line-height:1.6;
  display:-webkit-box;
  -webkit-line-clamp:2;
  -webkit-box-orient:vertical;
  overflow:hidden;
}

/* 审核区 */
.critic-section { margin-top:8px; }
.critic-title { font-size:16px; font-weight:600; margin:0 0 12px; }
.critic-title.pass { color:var(--success); }
.critic-title.blocked { color:var(--danger); }
.critic-title.needs-revision { color:var(--warning); }
.critic-summary { font-size:14px; line-height:1.7; color:var(--text-secondary); margin:0 0 20px; }
.critic-finding {
  border-left:3px solid var(--border-strong);
  padding:12px 16px;
  margin:14px 0;
  background:var(--canvas);
  border-radius:0 8px 8px 0;
}
.critic-code { font-size:12px; font-weight:600; color:var(--text-secondary); }
.critic-evidence { margin:6px 0 4px; font-size:13px; line-height:1.6; }
.critic-suggestion { margin:0; font-size:12px; color:var(--text-secondary); }

button:focus-visible, textarea:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }

@media(max-width:600px) {
  .field-card { padding:0 0 16px; }
  .plan-entry { padding:16px 0; }
}
</style>
