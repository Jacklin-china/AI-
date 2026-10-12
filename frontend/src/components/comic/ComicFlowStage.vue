<script setup lang="ts">
import { computed, ref } from 'vue'
import { AlertCircle, ArrowRight, Check, Columns2, Copy, Download, Film, ImageOff, Plus, RefreshCw, ShieldCheck, Trash2 } from 'lucide-vue-next'

/* 漫剧 04–07 四个制作阶段的共享视图组件。
   职责边界：只承担「展示 + 交互」；不做业务取数、不调用后端、不新建 Runtime/Prompt 系统。
   数据全部由 DirectorWorkspace 通过 props 注入（它已持有 projectPrompts / uploaded / shots 等真实数据）。

   重要真实性约束（design-spec §4.1 / §10）：
   后端当前不提供「图片分析报告」「图片版本链与选用关系」「故事实体」「服务端导出」，
   这些区块一律渲染为「待接入」提示，绝不显示「分析完成 / 优化完成 / 成片已生成」。
   能真实做的（复制文字、下载已登记的 Artifact）才提供按钮。 */
const props = defineProps<{
  stage: 'revision' | 'compare' | 'story' | 'export'
  projectPrompts: Record<string, unknown>[]
  uploaded: Record<string, unknown>[]
  pending: Record<string, unknown>[]
  featured: Record<string, unknown> | null
  selectedShot: string
  busy: boolean
}>()
const emit = defineEmits<{ copy: [text: string, hint: string]; go: [page: string] }>()

const copied = ref('')
const copyFailed = ref('')
/** 06 故事编排：镜头列表是本地会话状态（后端无 Story 实体），刷新不承诺持久化。 */
interface StoryShot { id: string; label: string; action: string; camera: string; duration: string; promptVersion?: number }
const storyShots = ref<StoryShot[]>([])

const titles: Record<string, { title: string; note: string }> = {
  revision: { title: '改图指令', note: '核对「本次改变」与「需要保留」，复制文字后连同原图一起交给外部工具。' },
  compare: { title: '版本比较', note: '完整比较原图与修改版。查看某个版本不等于选用它，选用由你确认。' },
  story: { title: '故事编排', note: '把选定图片编排成镜头：单图动态片段或多图连续故事。' },
  export: { title: '导出材料', note: '核对故事与图片对应关系，分别复制视频制作文字、下载图片素材包。' },
}
const heading = computed(() => titles[props.stage] ?? titles.revision!)

/** 有真实成图才允许进入后续阶段；无图时明确说明缺什么，不自动造图。 */
const hasImages = computed(() => props.uploaded.length > 0)
const imageName = computed(() => {
  const item = props.featured
  if (!item) return ''
  return `shot-${item.shot_sequence_number ?? '?'}-v${item.version ?? 1}.png`
})

/** 导出文字：只包含镜头与文件对应关系，明确不含图片本体。 */
const exportText = computed(() => {
  if (!props.uploaded.length) return ''
  const lines = props.uploaded.map((item, index) =>
    `镜头 ${item.shot_sequence_number ?? index + 1}｜文件 ${`shot-${item.shot_sequence_number ?? index + 1}-v${item.version ?? 1}.png`}｜Prompt v${item.version ?? 1}`)
  return ['漫剧制作材料', '以下为文字说明；图片需另行随附提交给视频工具。', '', ...lines].join('\n')
})

/** 04 改图指令：结构化卡片内容全部来自用户真实输入 + 已登记的真实成图信息。
    真实性约束：后端不提供「图片分析报告」「用户修改需求」「修改建议」实体，
    因此不预置任何编造文案；用户没写就显示空态提示，不伪造需求或建议。 */
const previewUrl = computed(() => (props.featured?.preview_url ? String(props.featured.preview_url) : ''))
/** 用户在 03 图片优化页或本页写下的修改诉求（本地会话状态，不承诺持久化）。 */
const requirement = ref('')
const revisionInstruction = computed(() => {
  const text = requirement.value.trim()
  if (!text || !hasImages.value) return ''
  return [
    `用户修改需求：${text}`,
    `请基于附上的原图（${imageName.value}）编辑。`,
    '需要保持不变：人物身份 · 服装与物件 · 画面构图 · 已确认的风格基调。',
  ].join('\n')
})

async function copyText(text: string, hint: string, key: string): Promise<void> {
  if (!text.trim()) return
  copied.value = ''; copyFailed.value = ''
  const ok = await copyToClipboard(text)
  if (ok) copied.value = key
  else { copyFailed.value = text; emit('copy', text, hint) }
}
async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return true }
  } catch { /* 回退到手动选择 */ }
  return false
}

/** 追加一张已选图片为一个镜头；排序提供上移/下移（不只靠拖动）。 */
function addShot(): void {
  const source = props.featured ?? props.uploaded[0]
  if (!source) return
  storyShots.value.push({
    id: `shot-${Date.now()}`,
    label: `镜头 ${storyShots.value.length + 1}`,
    action: '', camera: '', duration: '4s',
    promptVersion: Number(source.version ?? 1),
  })
}
function moveShot(index: number, step: number): void {
  const target = index + step
  if (target < 0 || target >= storyShots.value.length) return
  const list = storyShots.value
  const [item] = list.splice(index, 1)
  if (item) list.splice(target, 0, item)
}
function removeShot(index: number): void { storyShots.value.splice(index, 1) }
/** 冲突检查：同一镜头里「固定机位」与「缓慢推进」不能并存（design-spec §6 明确要求）。 */
function conflictOf(shot: StoryShot): string {
  const text = `${shot.camera}`
  if (text.includes('固定') && (text.includes('推进') || text.includes('推近'))) return '「固定机位」与「推进」冲突，请二选一。'
  return ''
}
</script>

<template>
  <section class="flow-stage" aria-label="制作阶段">
    <header class="flow-stage-head">
      <div><h3>{{ heading.title }}</h3><small>{{ heading.note }}</small></div>
      <button class="ui-button quiet sm" @click="emit('go', 'plan')">回到画面方案</button>
    </header>

    <!-- 04 改图指令（对齐《流程效果册》图三：右侧结构化卡片 + 复制按钮 + 等待导入） -->
    <template v-if="stage === 'revision'">
      <div class="rev-grid">
        <div class="rev-main">
          <!-- 必须附上原图：改图指令只有在「文字 + 原图」同时交给外部工具时才成立。 -->
          <section class="rev-card rev-attach">
            <span class="section-kicker">必须附上原图</span>
            <div class="rev-attach-body">
              <span class="rev-thumb">
                <img v-if="previewUrl" :src="previewUrl" :alt="imageName || '原图缩略图'" />
                <ImageOff v-else :size="18" />
              </span>
              <div class="rev-attach-meta">
                <strong>{{ imageName || '尚无已导入成图' }}</strong>
                <small>{{ hasImages ? '文字指令本身不含图片，需随附提交。' : '请先到「图片优化」导入成图。' }}</small>
              </div>
              <a v-if="previewUrl" class="ui-button sm" :href="previewUrl" :download="imageName || 'kantoku-shot.png'">
                <Download :size="14" /> 下载原图
              </a>
            </div>
          </section>

          <!-- 用户修改需求：由用户本人填写，不预置任何编造内容。 -->
          <section class="rev-card">
            <span class="section-kicker">用户修改需求</span>
            <textarea v-model="requirement" class="rev-input" rows="3"
              :disabled="!hasImages"
              :placeholder="hasImages ? '写下你想要的修改，如：光线柔和一点，保留人物和构图。' : '请先到「图片优化」导入成图。'"></textarea>
            <small v-if="!requirement.trim()" class="rev-hint">尚未填写修改需求。填好后下方会同步生成完整改图指令。</small>
          </section>

          <!-- 修改建议：后端无分析能力，只说明现状，不摆放编造建议。 -->
          <section class="rev-card">
            <span class="section-kicker">修改建议</span>
            <p class="rev-text rev-muted">暂无系统建议。图片分析能力接入后，这里会给出针对性的修改建议。</p>
          </section>

          <section class="rev-card rev-final">
            <span class="section-kicker">完整的改图指令</span>
            <pre v-if="revisionInstruction" class="rev-pre">{{ revisionInstruction }}</pre>
            <p v-else class="rev-text rev-muted">填写「用户修改需求」后，这里会生成可直接复制的完整改图指令。</p>
          </section>

          <section class="rev-card">
            <span class="section-kicker">需要保持不变</span>
            <p class="flow-keep"><ShieldCheck :size="15" /> 人物身份 · 服装与物件 · 画面构图 · 已确认的风格基调。调整保护内容需要明确操作。</p>
          </section>
        </div>

        <aside class="rev-side">
          <button class="ui-button primary" :disabled="!hasImages || !revisionInstruction || busy" @click="copyText(revisionInstruction, '改图指令', 'revision')">
            <Copy :size="15" /> 复制改图指令
          </button>
          <small v-if="!hasImages">需要先有真实成图才能整理改图指令。</small>
          <small v-else-if="!revisionInstruction">先填写「用户修改需求」。</small>
          <template v-else>
            <small>复制后请在外部工具同时附上原图。</small>
            <div class="rev-wait">
              <RefreshCw :size="15" />
              <div>
                <strong>等待导入修改版</strong>
                <p>在外部工具改完后，回到「版本比较」导入修改版，即可与原图并排核对。</p>
              </div>
            </div>
            <button class="ui-button quiet sm" @click="emit('go', 'compare')">去版本比较 <ArrowRight :size="14" /></button>
          </template>
        </aside>
      </div>
      <p v-if="copied === 'revision'" class="flow-feedback" role="status"><Check :size="14" /> 已复制文字，请在外部工具同时附上原图。</p>
      <p v-else-if="copyFailed" class="flow-feedback is-error" role="alert">复制失败，请手动选中下方正文复制。</p>
      <pre v-if="copyFailed" class="flow-pre">{{ copyFailed }}</pre>
    </template>

    <!-- 05 版本比较 -->
    <template v-else-if="stage === 'compare'">
      <div class="flow-compare">
        <figure class="flow-compare-pane">
          <figcaption><span class="flow-tag">原图 v1</span><small>已登记的作品图片</small></figcaption>
          <div class="flow-compare-canvas"><Check v-if="hasImages" :size="22" /><p>{{ hasImages ? '原图已导入' : '尚未导入图片' }}</p></div>
        </figure>
        <Columns2 class="flow-compare-divider" :size="18" />
        <figure class="flow-compare-pane">
          <figcaption><span class="flow-tag">修改版 v2</span><small>外部工具产出后导入</small></figcaption>
          <div class="flow-compare-canvas"><ImageOff :size="22" /><p>修改版尚未导入</p></div>
        </figure>
      </div>
      <section class="flow-decision">
        <div class="flow-decision-actions">
          <button class="ui-button primary" disabled>选用修改版作为作品图片</button>
          <button class="ui-button sm" disabled>保留原图</button>
          <button class="ui-button quiet sm" @click="emit('go', 'optimize')">继续修改</button>
        </div>
        <p class="flow-unavailable" role="status"><AlertCircle :size="14" /> 图片版本链与「选用关系」能力尚未接入，因此不提供真实选用操作，避免显示假的选用状态。</p>
      </section>
      <section class="flow-checklist">
        <span class="section-kicker">比较时要检查什么</span>
        <ul class="flow-list">
          <li><strong>要求是否已实现</strong><small>对照本次改变，逐条确认</small></li>
          <li><strong>保护内容是否漂移</strong><small>人物身份、服装、构图是否保持一致</small></li>
          <li><strong>是否有非目标改动</strong><small>多轮编辑可能带来非预期变化</small></li>
        </ul>
      </section>
    </template>

    <!-- 06 故事编排 -->
    <template v-else-if="stage === 'story'">
      <div class="flow-columns">
        <div class="flow-col">
          <div class="flow-col-head">
            <span class="section-kicker">镜头</span>
            <button class="ui-button sm" :disabled="!hasImages || busy" @click="addShot"><Plus :size="14" /> 添加选用图片</button>
          </div>
          <ol v-if="storyShots.length" class="flow-shots">
            <li v-for="(shot, index) in storyShots" :key="shot.id">
              <header>
                <strong>{{ shot.label }}</strong>
                <span class="flow-shot-actions">
                  <button class="ui-button quiet sm" :disabled="index === 0" aria-label="上移" @click="moveShot(index, -1)">↑</button>
                  <button class="ui-button quiet sm" :disabled="index === storyShots.length - 1" aria-label="下移" @click="moveShot(index, 1)">↓</button>
                  <button class="ui-button quiet sm" aria-label="移除" @click="removeShot(index)"><Trash2 :size="13" /></button>
                </span>
              </header>
              <label>动作<input v-model="shot.action" placeholder="小幅、自然的动作" /></label>
              <label>运镜<input v-model="shot.camera" placeholder="如：缓慢推进 / 固定机位" /></label>
              <label>时长<input v-model="shot.duration" placeholder="如 4s" /></label>
              <p v-if="conflictOf(shot)" class="flow-conflict" role="alert"><AlertCircle :size="13" /> {{ conflictOf(shot) }}</p>
            </li>
          </ol>
          <p v-else class="pane-note">还没有镜头。选择一张已导入的图片开始编排。</p>
        </div>
        <div class="flow-col">
          <span class="section-kicker">一致性检查</span>
          <ul class="flow-list">
            <li><strong>同一镜头不叠加冲突指令</strong><small>如「固定机位」与「缓慢推进」不并存</small></li>
            <li><strong>不同人物不自动视为同一角色</strong><small>未确认一致性的两张人像各自成镜</small></li>
            <li><strong>单图片段保留原构图</strong><small>优先小幅动作，不改画面身份</small></li>
          </ul>
          <p class="flow-unavailable" role="status"><AlertCircle :size="14" /> 故事与镜头实体尚未接入后端，当前编排保存在本次会话中，刷新不会持久化。</p>
        </div>
      </div>
    </template>

    <!-- 07 导出材料 -->
    <template v-else>
      <div class="flow-columns">
        <div class="flow-col">
          <span class="section-kicker">故事与镜头全文</span>
          <pre v-if="exportText" class="flow-pre">{{ exportText }}</pre>
          <p v-else class="pane-note">还没有可导出的镜头。请先在「故事编排」建立镜头，或先导入成图。</p>
        </div>
        <div class="flow-col">
          <span class="section-kicker">图片清单与镜头对应</span>
          <ul class="flow-list">
            <li v-for="(item, index) in uploaded" :key="String(item.artifact_id ?? index)">
              <strong>镜头 {{ item.shot_sequence_number ?? index + 1 }}</strong>
              <small>shot-{{ item.shot_sequence_number ?? index + 1 }}-v{{ item.version ?? 1 }}.png</small>
            </li>
            <li v-if="!uploaded.length"><strong>暂无已导入图片</strong><small>缺失图片会在此逐条列出，不会自动生成</small></li>
          </ul>
        </div>
      </div>
      <section class="flow-action">
        <button class="ui-button primary" :disabled="!exportText" @click="copyText(exportText, '视频制作文字', 'export')"><Copy :size="15" /> 复制视频制作文字</button>
        <button class="ui-button sm" disabled><Download :size="14" /> 下载图片素材包</button>
        <small>复制只包含文字；下载素材包能力尚未接入。</small>
      </section>
      <p v-if="copied === 'export'" class="flow-feedback" role="status"><Check :size="14" /> 已复制文字。到视频工具时请同时上传对应图片。</p>
      <p v-else-if="copyFailed" class="flow-feedback is-error" role="alert">复制失败，请手动选中上方正文复制。</p>
      <p class="flow-unavailable" role="status"><AlertCircle :size="14" /> 「视频制作材料已导出」不等于成片已生成；服务端素材包导出尚未接入。</p>
    </template>
  </section>
</template>

<style scoped>
/* 七步流程共享视图（04 改图指令 / 05 版本比较 / 06 故事编排 / 07 导出材料）。
   尺度一律取 tokens.css 令牌；留白按 --rhythm-* 拉开，避免 PDF 里那种"挤在一起"的观感。 */
.flow-stage { display: grid; gap: var(--space-8); }
.flow-stage-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); padding-bottom: var(--space-4); border-bottom: 1px solid var(--border-muted); }
.flow-stage-head h3 { margin:0; font-size: var(--font-section); font-weight: var(--weight-medium); line-height: 1.4; }
.flow-stage-head small { display: block; margin-top: var(--space-2); color: var(--text-secondary); font-size: var(--font-control); line-height: 1.65; }
.flow-columns { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-6); }
.flow-col { display: grid; align-content: start; gap: var(--space-3); }
.flow-col-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); }
.flow-list { display: grid; gap: var(--space-1); margin:0; padding:0; list-style: none; }
.flow-list li { display: grid; gap: var(--space-1); padding: var(--space-3) 0; border-bottom: 1px solid var(--border-muted); }
.flow-list li:last-child { border-bottom: 0; }
.flow-list strong { font-size: var(--font-control); font-weight: var(--weight-medium); }
.flow-list small { color: var(--text-muted); font-size: var(--font-meta); }
.flow-keep { display: flex; align-items: flex-start; gap: var(--space-3); margin:0; padding: var(--space-4); border-left: 3px solid var(--border-strong); background: var(--surface-subtle); color: var(--text-secondary); font-size: var(--font-control); line-height: 1.65; }
.flow-action { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); padding-top: var(--space-2); }
.flow-action small { color: var(--text-muted); font-size: var(--font-meta); }
.flow-feedback { display: flex; align-items: center; gap: var(--space-2); margin:0; color: var(--success); font-size: var(--font-control); }
.flow-feedback.is-error { color: var(--danger); }
.flow-pre { margin:0; padding: var(--space-4); overflow: auto; border: 1px solid var(--border-muted); border-radius: var(--radius-sm); background: var(--surface-subtle); color: var(--text-secondary); font: var(--font-control)/1.7 var(--font-mono); white-space: pre-wrap; }
.flow-compare { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: var(--space-4); }
.flow-compare-pane { display: grid; gap: var(--space-3); margin:0; }
.flow-compare-pane figcaption { display: flex; align-items: center; gap: var(--space-2); }
.flow-compare-pane figcaption small { color: var(--text-muted); font-size: var(--font-meta); }
.flow-tag { padding: var(--space-1) var(--space-2); border: 1px solid var(--border-strong); border-radius: var(--radius-pill); color: var(--text-primary); font-size: var(--font-meta); }
.flow-compare-canvas { display: grid; place-items: center; align-content: center; gap: var(--space-2); min-height: 240px; border: 1px solid var(--border-muted); border-radius: var(--radius-tile); background: var(--canvas-inset); color: var(--text-muted); }
.flow-compare-canvas p { margin:0; font-size: var(--font-control); }
.flow-compare-divider { color: var(--text-muted); }
.flow-decision { display: grid; gap: var(--space-3); padding-top: var(--space-2); }
.flow-decision-actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
.flow-unavailable { display: flex; align-items: flex-start; gap: var(--space-2); margin:0; padding: var(--space-3); border-left: 3px solid var(--warning-border); background: var(--warning-soft); color: var(--text-secondary); font-size: var(--font-control); line-height: 1.6; }
.flow-checklist { display: grid; gap: var(--space-3); padding-top: var(--space-3); border-top: 1px solid var(--border-muted); }
.flow-shots { display: grid; gap: var(--space-3); margin:0; padding:0; list-style: none; }
.flow-shots > li { display: grid; gap: var(--space-3); padding: var(--space-5); border: 1px solid var(--border-muted); border-radius: var(--radius-sm); background: var(--surface); }
.flow-shots header { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); }
.flow-shots strong { font-size: var(--font-control); font-weight: var(--weight-medium); }
.flow-shot-actions { display: flex; gap: var(--space-1); }
.flow-shots label { display: grid; gap: var(--space-1); color: var(--text-secondary); font-size: var(--font-meta); }
.flow-shots input { height: var(--control-md); padding:0 var(--space-2); border: 1px solid var(--border-strong); border-radius: var(--radius-control); background: #fff; color: var(--text-primary); font: inherit; font-size: var(--font-control); }
.flow-shots input:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); outline-offset: 1px; }
.flow-conflict { display: flex; align-items: center; gap: var(--space-1); margin:0; color: var(--danger); font-size: var(--font-meta); }

/* ---- 04 改图指令（图三版式）：左侧结构化卡片序列 + 右侧操作栏 ---- */
.rev-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(240px, 300px); gap: var(--space-6); align-items: start; }
.rev-main { display: grid; gap: var(--space-4); }
.rev-card { display: grid; gap: var(--space-3); padding: var(--space-5); border: 1px solid var(--border-muted); border-radius: var(--radius-panel); background: var(--surface); }
.rev-attach-body { display: flex; align-items: center; gap: var(--space-4); }
.rev-thumb { display: grid; place-items: center; flex-shrink: 0; width: 64px; height: 64px; overflow: hidden; border: 1px solid var(--border-muted); border-radius: var(--radius-tile); background: var(--canvas-inset); color: var(--text-muted); }
.rev-thumb img { width: 100%; height: 100%; object-fit: cover; }
.rev-attach-meta { display: grid; gap: var(--space-1); min-width: 0; }
.rev-attach-meta strong { font-size: var(--font-control); font-weight: var(--weight-medium); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rev-attach-meta small { color: var(--text-muted); font-size: var(--font-meta); line-height: 1.6; }
.rev-text { margin:0; color: var(--text-secondary); font-size: var(--font-body); line-height: 1.7; }
.rev-text.rev-muted { color: var(--text-muted); font-size: var(--font-meta); }
.rev-input { width:100%; padding: var(--space-3); border:1px solid var(--border-strong); border-radius: var(--radius-control); background:#fff; color: var(--text-primary); font: inherit; font-size: var(--font-control); line-height: 1.65; resize: vertical; }
.rev-input:disabled { background: var(--surface-subtle); color: var(--text-muted); cursor: not-allowed; }
.rev-hint { color: var(--text-muted); font-size: var(--font-kicker); }
.rev-final { border-color: var(--border-strong); background: var(--surface-subtle); }
.rev-pre { margin:0; color: var(--text-primary); font-size: var(--font-control); line-height: 1.75; white-space: pre-wrap; }
.rev-side { display: grid; gap: var(--space-3); padding: var(--space-5); border: 1px solid var(--border-muted); border-radius: var(--radius-panel); background: var(--surface-subtle); }
.rev-side small { color: var(--text-muted); font-size: var(--font-meta); line-height: 1.6; }
.rev-wait { display: flex; align-items: flex-start; gap: var(--space-3); padding: var(--space-4); border-left: 3px solid var(--border-strong); background: var(--surface); color: var(--text-secondary); }
.rev-wait strong { display: block; font-size: var(--font-control); font-weight: var(--weight-medium); color: var(--text-primary); }
.rev-wait p { margin: var(--space-1) 0 0; font-size: var(--font-meta); line-height: 1.6; }
@media (max-width: 1100px) { .flow-columns, .flow-compare, .rev-grid { grid-template-columns: 1fr; } .flow-compare-divider { display: none; } }
</style>
