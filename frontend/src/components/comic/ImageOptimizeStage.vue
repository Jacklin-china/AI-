<script setup lang="ts">
import { computed, ref } from 'vue'
import { Check, ChevronDown, ImagePlus, Lock, Paperclip, Sparkles, Wand2 } from 'lucide-vue-next'

/* 03 图片优化 阶段的独立视图（对齐《流程效果册》图二版式）。
   职责边界：只承担「展示 + 交互」；不做业务取数、不调用后端、不新建 Runtime / Prompt 系统。
   数据全部由 DirectorWorkspace 通过 props 注入（它已持有 uploadedPrompts / featuredPrompt 等真实数据）。

   真实性约束（design-spec §4.1 / §10，与 ComicFlowStage 一致）：
   后端当前不提供「图片分析报告」，因此右侧「可改善点」一律标注为「示例」，
   绝不显示「分析完成」；能真实做的（复制文字、登记已导入图片）才提供按钮。 */

const props = defineProps<{
  uploaded: Record<string, unknown>[]
  featured: Record<string, unknown> | null
  selectedShot: string
  busy: boolean
}>()
const emit = defineEmits<{ copy: [text: string, hint: string]; go: [page: string] }>()

const copied = ref(false)

/** 真实已导入成图才允许整理指令；无图时明确说明缺什么，不自动造图。 */
const hasImages = computed(() => props.uploaded.length > 0)
const imageName = computed(() => {
  const item = props.featured
  if (!item) return ''
  return `shot-${item.shot_sequence_number ?? '?'}-v${item.version ?? 1}.png`
})

/** 「保留」约束：默认折叠为一行摘要，展开可读全文。 */
const keepOpen = ref(false)
const keepSummary = '人物、表情、手势、服装、饮品与构图'

/** 用户补充的修改诉求（本地会话状态，不承诺持久化）。 */
const requirement = ref('')

/** 可改善点：当前后端无分析能力，用「示例」占位说明版式与交互，
   不假装是真实分析结果。 */
interface Improvement { id: number; title: string; evidence: string; suggestion: string; tag: string }
const samples: Improvement[] = [
  { id: 1, title: '上方背景高光偏亮', evidence: '人物上方逆光区域较亮。', suggestion: '降低高光，保留逆光氛围。', tag: '定位可调整 · 忽略' },
  { id: 2, title: '右侧车辆较抢眼', evidence: '右侧的车辆较为醒目。', suggestion: '可适当降低车辆的存在感。', tag: '定位可调整 · 忽略' },
]
const selected = ref<number[]>([1])
function toggleSample(id: number): void {
  selected.value = selected.value.includes(id) ? selected.value.filter(item => item !== id) : [...selected.value, id]
}

/** 整理修改指令：只产出文字，明确不含图片本体；真正的改图在外部工具完成。 */
async function organize(): Promise<void> {
  const parts: string[] = []
  if (requirement.value.trim()) parts.push(`用户修改需求：${requirement.value.trim()}`)
  const picked = samples.filter(item => selected.value.includes(item.id))
  if (picked.length) parts.push(`修改建议：${picked.map(item => item.suggestion).join(' ')}`)
  if (imageName.value) parts.push(`请基于附上的原图（${imageName.value}）编辑。`)
  parts.push(`需要保持不变：${keepSummary}。`)
  const text = parts.join('\n')
  const ok = await copyToClipboard(text)
  copied.value = ok
  emit('copy', text, '修改指令')
}
async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return true }
  } catch { /* 回退：由父级统一处理选中复制 */ }
  return false
}
</script>

<template>
  <section class="optimize-stage" aria-label="图片优化">
    <header class="opt-head">
      <div>
        <h3>{{ hasImages ? (featured && featured.shot_label ? String(featured.shot_label) : '镜头画面') : '导入图片，选择需要改善的地方' }}</h3>
        <small>图片优化 · {{ hasImages ? '原图已导入' : '尚未导入成图' }}</small>
      </div>
      <button class="ui-button quiet sm" @click="emit('go', 'prompt')"><Sparkles :size="14" /> 查看提示词</button>
    </header>

    <div class="opt-grid">
      <!-- 中间：大图预览 + 编号热点 -->
      <figure class="opt-canvas">
        <div class="opt-canvas-frame">
          <div v-if="hasImages" class="opt-canvas-media" :aria-label="imageName">
            <slot name="preview" />
          </div>
          <div v-else class="opt-canvas-empty">
            <ImagePlus :size="26" />
            <p>尚未导入成图</p>
            <button class="ui-button sm" @click="emit('go', 'prompt')">到生图指令准备图片</button>
          </div>
          <span v-for="point in samples" :key="point.id" class="opt-hotspot" :data-index="point.id">{{ point.id }}</span>
        </div>
        <figcaption>
          <span>{{ imageName || '参考图片预览 · 设计素材' }}</span>
          <span class="opt-caption-thumb">
            <span class="opt-thumb" v-if="hasImages"><slot name="thumb" /></span>
            <span class="opt-thumb-empty" v-else><ImagePlus :size="14" /></span>
            <span class="opt-upload">
              <button class="ui-button sm" :disabled="busy" @click="emit('go', 'prompt')"><Paperclip :size="13" /> 导入图片</button>
              <small>PNG/JPG/WebP · 不超过 5MB</small>
            </span>
          </span>
        </figcaption>
      </figure>

      <!-- 右侧：可改善点 + 保留约束 + 整理指令 -->
      <aside class="opt-panel">
        <div class="opt-panel-head">
          <h4>可改善点（示例）</h4>
          <small>示例分析</small>
        </div>
        <ol class="opt-list">
          <li v-for="point in samples" :key="point.id" :class="{ 'is-picked': selected.includes(point.id) }">
            <span class="opt-index">{{ point.id }}</span>
            <div class="opt-body">
              <strong>{{ point.title }}</strong>
              <div class="opt-evidence">
                <span class="opt-evidence-thumb" aria-hidden="true"></span>
                <p>{{ point.evidence }}</p>
              </div>
              <div class="opt-suggest">
                <span>修改建议</span>
                <p>{{ point.suggestion }}</p>
              </div>
              <label class="opt-pick">
                <input type="checkbox" :checked="selected.includes(point.id)" @change="toggleSample(point.id)" />
                <span>加入本次修改</span>
              </label>
              <small class="opt-tag">{{ point.tag }}</small>
            </div>
          </li>
        </ol>

        <details class="opt-keep" :open="keepOpen" @toggle="keepOpen = ($event.target as HTMLDetailsElement).open">
          <summary><Lock :size="14" /> 保留{{ keepSummary }}<ChevronDown :size="14" class="opt-keep-arrow" /></summary>
          <p>光线柔和一点，保留人物和构图。</p>
        </details>

        <div class="opt-compose">
          <textarea v-model="requirement" class="opt-input" rows="2" placeholder="补充你的修改诉求，如：光线柔和一点，保留人物和构图。"></textarea>
          <div class="opt-compose-actions">
            <Paperclip :size="15" class="opt-clip" />
            <button class="ui-button primary" :disabled="!hasImages || busy" @click="organize">
              <Wand2 :size="15" /> 整理修改指令
            </button>
          </div>
        </div>
        <p class="opt-next">
          <Check v-if="copied" :size="13" />
          下一步：确认修改范围，复制指令到外部工具改图。
        </p>
        <p v-if="!hasImages" class="opt-next is-muted">需要先有真实成图才能整理修改指令。</p>
      </aside>
    </div>
  </section>
</template>

<style scoped>
/* 三栏式：左侧制作步骤由外层 WorkspaceShell 承担，这里只负责「大图 + 右面板」两栏。
   尺度一律取 tokens 令牌；留白按 --space-* 拉开，与 PDF 的疏朗版式一致。 */
.optimize-stage { display: grid; gap: var(--space-6); }
.opt-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); }
.opt-head h3 { margin: 0; font-size: var(--font-section); font-weight: var(--weight-medium); line-height: 1.35; }
.opt-head small { display: block; margin-top: var(--space-1); color: var(--text-muted); font-size: var(--font-meta); }

.opt-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(300px, 360px); gap: var(--space-6); align-items: start; }

/* ---- 中间大图 ---- */
.opt-canvas { margin: 0; display: grid; gap: var(--space-3); }
.opt-canvas-frame { position: relative; min-height: 420px; display: grid; place-items: center; overflow: hidden; border: 1px solid var(--border-muted); border-radius: var(--radius-panel); background: var(--canvas-inset); }
.opt-canvas-media { width: 100%; height: 100%; }
.opt-canvas-media :deep(img) { width: 100%; height: 100%; max-height: 60vh; object-fit: contain; display: block; }
.opt-canvas-empty { display: grid; place-items: center; gap: var(--space-2); padding: var(--space-8); color: var(--text-muted); text-align: center; }
.opt-canvas-empty p { margin: 0; font-size: var(--font-control); }
.opt-hotspot { position: absolute; display: grid; place-items: center; width: 22px; height: 22px; border-radius: var(--radius-pill); background: rgba(20,24,31,.62); color: #fff; font-size: var(--font-kicker); font-weight: 600; }
.opt-hotspot[data-index="1"] { top: 12%; right: 14%; }
.opt-hotspot[data-index="2"] { bottom: 32%; right: 8%; }
.opt-canvas figcaption { display: grid; gap: var(--space-3); color: var(--text-muted); font-size: var(--font-kicker); }
.opt-caption-thumb { display: flex; align-items: center; gap: var(--space-3); padding: var(--space-3); border: 1px solid var(--border-muted); border-radius: var(--radius-control); background: var(--surface); }
.opt-thumb, .opt-thumb-empty { display: grid; place-items: center; width: 44px; height: 44px; flex: 0 0 auto; overflow: hidden; border: 1px solid var(--border-muted); border-radius: var(--radius-control); background: var(--canvas-inset); color: var(--text-muted); }
.opt-thumb :deep(img) { width: 100%; height: 100%; object-fit: cover; }
.opt-upload { display: flex; align-items: center; gap: var(--space-3); }
.opt-upload small { color: var(--text-muted); font-size: var(--font-kicker); }

/* ---- 右侧面板 ---- */
.opt-panel { display: grid; gap: var(--space-5); }
.opt-panel-head h4 { margin: 0; font-size: var(--font-body); font-weight: var(--weight-medium); color: var(--text-primary); }
.opt-panel-head small { display: block; margin-top: var(--space-1); color: var(--text-muted); font-size: var(--font-kicker); }

.opt-list { display: grid; gap: var(--space-3); margin: 0; padding: 0; list-style: none; }
.opt-list > li { display: grid; grid-template-columns: auto 1fr; gap: var(--space-3); padding: var(--space-4); border: 1px solid var(--border-muted); border-radius: var(--radius-control); background: var(--surface); transition: border-color var(--duration-fast) var(--ease-standard), box-shadow var(--duration-fast) var(--ease-standard); }
.opt-list > li.is-picked { border-color: var(--accent-ring); box-shadow: 0 0 0 2px var(--accent-ring); }
.opt-index { display: grid; place-items: center; width: 20px; height: 20px; border-radius: var(--radius-pill); background: var(--canvas-inset); color: var(--text-secondary); font-size: var(--font-kicker); font-weight: 600; }
.opt-body { display: grid; gap: var(--space-2); min-width: 0; }
.opt-body strong { font-size: var(--font-control); font-weight: var(--weight-medium); color: var(--text-primary); }
.opt-evidence { display: grid; grid-template-columns: 62px 1fr; gap: var(--space-3); align-items: center; }
.opt-evidence-thumb { height: 40px; border-radius: var(--radius-tile); background: linear-gradient(135deg, var(--canvas-inset), var(--border-muted)); }
.opt-evidence p { margin: 0; color: var(--text-secondary); font-size: var(--font-meta); line-height: var(--line-body); }
.opt-suggest { display: grid; gap: var(--space-1); }
.opt-suggest > span { color: var(--text-muted); font-size: var(--font-kicker); }
.opt-suggest p { margin: 0; color: var(--text-secondary); font-size: var(--font-meta); line-height: var(--line-body); }
.opt-pick { display: inline-flex; align-items: center; gap: var(--space-2); color: var(--text-secondary); font-size: var(--font-meta); cursor: pointer; }
.opt-tag { color: var(--text-muted); font-size: var(--font-kicker); }

.opt-keep { border: 1px solid var(--border-muted); border-radius: var(--radius-control); background: var(--surface); }
.opt-keep summary { display: flex; align-items: center; gap: var(--space-2); padding: var(--space-3) var(--space-4); cursor: pointer; color: var(--text-secondary); font-size: var(--font-meta); }
.opt-keep-arrow { margin-left: auto; transition: transform var(--duration-fast) var(--ease-out); }
.opt-keep[open] .opt-keep-arrow { transform: rotate(180deg); }
.opt-keep p { margin: 0; padding: 0 var(--space-4) var(--space-4); color: var(--text-secondary); font-size: var(--font-meta); line-height: var(--line-body); }

.opt-compose { display: grid; gap: var(--space-2); padding: var(--space-3); border: 1px solid var(--border-strong); border-radius: var(--radius-control); background: var(--surface); }
.opt-input { width: 100%; border: 0; outline: 0; resize: vertical; background: transparent; color: var(--text-primary); font: inherit; font-size: var(--font-meta); line-height: var(--line-body); }
.opt-compose-actions { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); }
.opt-clip { color: var(--text-muted); }
.opt-next { display: flex; align-items: center; gap: var(--space-1); margin: 0; color: var(--text-muted); font-size: var(--font-kicker); }
.opt-next.is-muted { color: var(--text-muted); }

@media (max-width: 900px) {
  .opt-grid { grid-template-columns: 1fr; }
  .opt-canvas-frame { min-height: 280px; }
}
</style>
