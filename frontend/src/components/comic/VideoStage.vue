<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Info, Lock, Play, RotateCcw, Sparkles } from 'lucide-vue-next'
import type { ComicImageRef, ComicImageView } from '../../services/core'
import { storyImageKey, type StoryWorkbench } from '../../domains/comic/storyWorkbench'

/* 视频生成阶段（界面壳）。
   输入严格来自「导出材料」：故事确认后的镜头清单 + 每个镜头的图片，不在这一层重新选图。
   后端 VideoService 目前没有开放 HTTP 路由（`video_enabled` 默认关闭、Provider 为 Mock），
   因此这里只实现界面与动效：参数是本地状态、不落库；「生成视频」显式禁用；
   进度条是本地演示数据，并在界面上标注「演示」，不写入产物、不产生计费。 */
const props = defineProps<{ workbench: StoryWorkbench }>()
const emit = defineEmits<{ go: [page: string] }>()

const state = computed(() => props.workbench.state)

interface VideoShot {
  key: string
  index: number
  action: string
  camera: string
  duration: number | null
  filename: string
  image: ComicImageRef
}
/* 预览键与可用性都来自现有图片视图，不新建数据结构。 */
type ResolvedImage = ComicImageView

/* 优先用导出清单（manifest）——它就是导出材料里的那份；未导出时退回故事草稿镜头。 */
const shots = computed<VideoShot[]>(() => {
  const manifest = state.value.view?.manifest?.shots ?? []
  if (manifest.length) {
    return manifest.map(shot => ({
      key: shot.shot_id,
      index: shot.sequence_number,
      action: shot.action,
      camera: shot.camera,
      duration: shot.duration_seconds,
      filename: shot.filename,
      image: { image_id: shot.image_id, version: shot.image_version },
    }))
  }
  return state.value.draft.shots.map((shot, position) => ({
    key: shot.shot_id,
    index: position + 1,
    action: shot.action,
    camera: shot.camera,
    duration: shot.duration_seconds,
    filename: '',
    image: shot.image,
  }))
})

function resolveImage(image: ComicImageRef): ResolvedImage | undefined {
  const key = storyImageKey(image)
  return state.value.view?.story?.fixed_images.find(item => storyImageKey(item) === key)
    ?? state.value.images.find(item => storyImageKey(item) === key)
}

const resolved = computed(() => shots.value.map(shot => resolveImage(shot.image)))
const selected = ref(0)
const current = computed<VideoShot | undefined>(() => shots.value[selected.value] ?? shots.value[0])
const currentImage = computed(() => resolved.value[selected.value] ?? resolved.value[0])
const frameSrc = computed(() => {
  const image = currentImage.value
  return image ? state.value.previews[image.artifact_id] ?? '' : ''
})
const frameError = computed(() => {
  const image = currentImage.value
  if (!image) return '这个镜头还没有关联图片，请回到「故事编排」选图。'
  if (image.available === false) return '这张图片已不可用，请回到「图片工作台」选择有效版本。'
  if (!frameSrc.value) return state.value.previewErrors[image.artifact_id] || '正在读取首帧图片…'
  return ''
})

/* 让故事控制器把这一阶段要用的图片读进预览缓存（与故事页同一套，不重复实现）。 */
watch(
  () => resolved.value.map(image => image?.artifact_id ?? '').join(','),
  () => {
    const images = resolved.value.filter((image): image is ResolvedImage => !!image && image.available !== false)
    if (images.length) void props.workbench.showImages(images)
  },
  { immediate: true },
)

watch(() => shots.value.length, () => { if (selected.value >= shots.value.length) selected.value = 0 })

/* ---------------- 演示用进度 ----------------
   真实进度要等后端提供任务查询；这里的数字只用于看动效，绝不冒充真实状态。 */
const DEMO_DURATION = 8600
const demo = ref<'idle' | 'running' | 'done'>('idle')
const progress = ref(0)
const elapsed = ref(0)
let raf = 0
let startedAt = 0

function stopDemo(): void {
  if (raf) cancelAnimationFrame(raf)
  raf = 0
}
function startDemo(): void {
  stopDemo()
  demo.value = 'running'
  progress.value = 0
  elapsed.value = 0
  startedAt = performance.now()
  const step = (now: number): void => {
    const ratio = Math.min(1, (now - startedAt) / DEMO_DURATION)
    progress.value = ratio * 100
    elapsed.value = ratio * 8
    if (ratio >= 1) { demo.value = 'done'; raf = 0; return }
    raf = requestAnimationFrame(step)
  }
  raf = requestAnimationFrame(step)
}
function resetDemo(): void {
  stopDemo()
  demo.value = 'idle'
  progress.value = 0
  elapsed.value = 0
}
onBeforeUnmount(stopDemo)

const STAGES: [number, string][] = [
  [0, '解析首帧与镜头提示词…'],
  [0.26, '构建运动轨迹…'],
  [0.52, '渲染第 4 秒的画面…'],
  [0.8, '收尾与色彩对齐…'],
]
const stageText = computed(() => {
  const ratio = progress.value / 100
  let text = STAGES[0]![1]
  for (const [threshold, label] of STAGES) if (ratio >= threshold) text = label
  return text
})
const frameNumber = computed(() => String(Math.round((progress.value / 100) * 192)).padStart(3, '0'))
const percentLabel = computed(() => `${Math.round(progress.value)}%`)
const etaLabel = computed(() => String(Math.max(0, Math.ceil((1 - progress.value / 100) * 13))))

/* ---------------- 参数：本地状态，不落库 ---------------- */
const model = ref('xiaoyunque-1.5')
const duration = ref(8)
const camera = ref('推近')
const MODELS = [
  { value: 'xiaoyunque-1.5', label: '小云雀 Video 1.5' },
  { value: 'jimeng-seedance', label: '即梦 Seedance' },
]
const CAMERAS = ['推近', '拉远', '横移', '固定']
</script>

<template>
  <section class="vs" aria-label="视频生成">
    <!-- 外层工作台页眉在这一阶段被隐藏，标题由本阶段自己给，避免同屏出现两个「视频生成」。 -->
    <div class="vs-head">
      <h3>视频生成</h3>
      <span class="vs-tag">界面壳 · 未接后端</span>
      <span v-if="demo !== 'idle'" class="vs-tag is-demo">演示数据</span>
      <p class="vs-sub">输入来自上一步「导出材料」：镜头提示词与镜头图，进入这一阶段不再重新选图。</p>
    </div>

    <p class="vs-notice" role="status">
      <Info :size="14" />
      <span>
        后端视频能力（VideoService）尚未开放接口，本阶段只实现界面：参数可选、加载动效可演示，
        <b>不会发起任何生成请求</b>，不写产物、不产生费用；进度为本地演示数据。
      </span>
    </p>

    <div class="vs-grid">
      <div class="vs-canvas">
        <!-- 镜头选择 -->
        <div class="vs-shots" role="tablist" aria-label="选择镜头">
          <button
            v-for="(shot, position) in shots"
            :key="shot.key"
            type="button"
            role="tab"
            class="vs-shot"
            :aria-selected="position === selected"
            :disabled="demo === 'running'"
            @click="selected = position"
          >
            <span class="vs-shot-no">{{ String(shot.index).padStart(2, '0') }}</span>
            <span class="vs-shot-text">{{ shot.camera || '未设运镜' }} · {{ shot.duration ?? '—' }}s</span>
          </button>
          <span v-if="!shots.length" class="vs-shot-empty">还没有镜头，先完成「故事编排」并导出材料</span>
        </div>

        <!-- 首尾帧 -->
        <div class="vs-rails">
          <div class="vs-rail">
            <img v-if="frameSrc" :src="frameSrc" alt="" />
            <span v-else class="vs-rail-empty" aria-hidden="true"></span>
            <div class="vs-rail-text">
              <b>首帧</b>
              <small><Lock :size="10" /> 来自导出材料</small>
            </div>
          </div>
          <span class="vs-rail-arrow" aria-hidden="true">→</span>
          <div class="vs-rail is-empty">
            <span class="vs-rail-empty" aria-hidden="true"></span>
            <div class="vs-rail-text">
              <b>尾帧</b>
              <small>未设定</small>
            </div>
          </div>
          <span class="vs-rail-note">首尾帧决定这一镜怎么收；尾帧要等后端视频能力接入</span>
        </div>

        <!-- 平板播放器 -->
        <div class="vs-player" :class="{ 'is-demo': demo !== 'idle', 'is-done': demo === 'done' }" :style="{ '--vp': `${progress}%` }">
          <template v-if="frameSrc">
            <img class="vs-frame vs-frame-base" :src="frameSrc" :alt="`镜头 ${current?.index ?? 1} 的首帧`" />
            <div v-if="demo === 'running'" class="vs-reveal">
              <img class="vs-frame" :src="frameSrc" alt="" />
            </div>
          </template>
          <p v-else class="vs-empty">{{ frameError }}</p>

          <span v-if="demo === 'running'" class="vs-glow" aria-hidden="true"></span>
          <span v-if="demo === 'running'" class="vs-band" aria-hidden="true"></span>
          <span v-if="demo === 'running'" class="vs-tlabel">{{ elapsed.toFixed(1) }}s</span>

          <!-- 会话生成卡：状态长在消息里，而不是盖满画面 -->
          <div v-if="demo !== 'idle'" class="vs-card" role="status">
            <span class="vs-avatar" aria-hidden="true">雀</span>
            <div class="vs-bubble">
              <div class="vs-bubble-row">
                <span class="vs-thumb">
                  <img v-if="frameSrc" :src="frameSrc" alt="" />
                </span>
                <div class="vs-bubble-text">
                  <div class="vs-bubble-title">
                    镜头 {{ String(current?.index ?? 1).padStart(2, '0') }} ·
                    {{ demo === 'running' ? '生成中' : '演示完成' }}
                    <em>v1</em>
                  </div>
                  <div class="vs-bubble-stage">
                    <s v-if="demo === 'running'">{{ stageText }}</s>
                    <span v-else>未写入任何版本 · 仅演示</span>
                  </div>
                  <div class="vs-track"><i :style="{ width: `${progress}%` }"></i></div>
                </div>
              </div>
              <div class="vs-bubble-foot">
                <span>第 {{ elapsed.toFixed(1) }} / 8.0 秒 · 帧 {{ frameNumber }} / 192</span>
                <span class="vs-bubble-right">{{ percentLabel }} · 约剩 {{ etaLabel }} 秒</span>
              </div>
            </div>
          </div>

          <span v-if="demo === 'done'" class="vs-done-tag">演示结束 · 未接后端</span>
        </div>

        <!-- 操作 -->
        <div class="vs-actions">
          <button
            class="ui-button primary"
            type="button"
            disabled
            title="后端视频能力（VideoService）尚未开放接口，当前版本不会发起生成请求"
          >
            生成视频
          </button>
          <button v-if="demo === 'idle'" class="ui-button" type="button" :disabled="!frameSrc" @click="startDemo">
            <Play :size="14" /> 演示加载动效
          </button>
          <button v-else class="ui-button" type="button" @click="resetDemo">
            <RotateCcw :size="14" /> 重播
          </button>
          <button class="ui-button quiet" type="button" @click="emit('go', 'export')">回到导出材料</button>
          <span class="vs-actions-note">
            <Sparkles :size="12" /> 动效采用「时间波前 + 会话生成卡」：画面从首帧推向尾帧，状态写在消息里
          </span>
        </div>

        <!-- 队列 -->
        <div class="vs-queue">
          <span class="vs-dot" :class="{ 'is-live': demo === 'running' }" aria-hidden="true"></span>
          <span v-if="demo === 'running'">镜头 {{ String(current?.index ?? 1).padStart(2, '0') }} 演示中 {{ percentLabel }} · 预计 {{ etaLabel }} 秒</span>
          <span v-else>队列 0 · 任务通道未接入，暂不排队</span>
          <span class="vs-queue-right">可离开本页；接入后完成后回到这条会话查看</span>
        </div>
      </div>

      <!-- 参数 -->
      <aside class="vs-side" aria-label="模型与参数">
        <div class="vs-side-head">
          <b>模型与参数</b>
          <small>本地选择 · 不保存</small>
        </div>

        <label class="vs-field">
          <span>模型</span>
          <select v-model="model" :disabled="demo === 'running'">
            <option v-for="item in MODELS" :key="item.value" :value="item.value">{{ item.label }}</option>
          </select>
        </label>

        <div class="vs-field">
          <span>时长</span>
          <div class="vs-seg">
            <button v-for="value in [4, 8]" :key="value" type="button" :aria-pressed="duration === value"
              :disabled="demo === 'running'" @click="duration = value">{{ value }}s</button>
          </div>
        </div>

        <div class="vs-field">
          <span>运镜</span>
          <div class="vs-seg vs-seg-wrap">
            <button v-for="value in CAMERAS" :key="value" type="button" :aria-pressed="camera === value"
              :disabled="demo === 'running'" @click="camera = value">{{ value }}</button>
          </div>
        </div>

        <label class="vs-field">
          <span>画面比例</span>
          <input value="4:3 · 跟随首帧" disabled />
          <small>由首帧图片决定，不单独设置</small>
        </label>

        <dl class="vs-facts">
          <div><dt>输入</dt><dd>{{ shots.length }} 个镜头 · 首帧已锁定</dd></div>
          <div><dt>预估费用</dt><dd>— · 未接入计费</dd></div>
          <div><dt>输出</dt><dd>— · 未接入产物</dd></div>
        </dl>

        <p class="vs-side-note">
          接入时这里会显示：`estimated_fen` 预估、`max_fen` 上限，以及按 `request_id` 幂等的提交结果。
        </p>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.vs { display:flex; flex-direction:column; gap:var(--space-3); min-width:0; }
.vs-head { display:flex; align-items:center; gap:var(--space-2); flex-wrap:wrap; }
.vs-head h3 { margin:0 var(--space-1) 0 0; font-size:var(--font-section); font-weight:600; }
.vs-tag { padding:1px 8px; border-radius:var(--radius-pill); background:var(--surface-subtle); border:1px solid var(--border-muted); font-size:var(--font-kicker); color:var(--text-muted); }
.vs-tag.is-demo { background:var(--warning-surface, #fbf1e0); border-color:#efdfc2; color:var(--warning); }
.vs-sub { margin:0; font-size:var(--font-kicker); color:var(--text-muted); }

.vs-notice { display:flex; gap:var(--space-2); margin:0; padding:var(--space-3);
  border:1px solid var(--border-muted); border-radius:var(--radius-panel); background:var(--surface-subtle);
  font-size:var(--font-control); line-height:1.6; color:var(--text-secondary); }
.vs-notice svg { flex-shrink:0; margin-top:2px; color:var(--text-muted); }
.vs-notice b { color:var(--text-primary); }

/* 播放器按 616px 上限居中，镜头条与首尾帧仍占满画布列，宽屏不会留下空档。 */
.vs-grid { display:grid; grid-template-columns:minmax(0, 1fr) 296px; gap:var(--space-5); align-items:start; }
.vs-canvas { display:flex; flex-direction:column; gap:var(--space-3); min-width:0; }

/* 镜头选择 */
.vs-shots { display:flex; gap:var(--space-2); overflow-x:auto; padding-bottom:2px; scrollbar-width:thin; }
.vs-shot { display:inline-flex; align-items:center; gap:var(--space-2); flex-shrink:0;
  padding:var(--space-2) var(--space-3); border:1px solid var(--border-muted); border-radius:var(--radius-pill);
  background:var(--surface); color:var(--text-secondary); font:inherit; font-size:var(--font-control); cursor:pointer; }
.vs-shot[aria-selected="true"] { background:var(--sidebar-selected); border-color:var(--border-strong, #c8ccd2); color:var(--text-primary); font-weight:500; }
.vs-shot:disabled { opacity:.6; cursor:not-allowed; }
.vs-shot-no { font-variant-numeric:tabular-nums; color:var(--text-muted); }
.vs-shot-empty { font-size:var(--font-control); color:var(--text-muted); }

/* 首尾帧 */
.vs-rails { display:flex; align-items:center; gap:var(--space-3); flex-wrap:wrap; }
.vs-rail { display:flex; align-items:center; gap:var(--space-2); padding:var(--space-1) var(--space-3) var(--space-1) var(--space-1);
  border:1px solid var(--border-muted); border-radius:var(--radius-dialog); background:var(--surface); }
.vs-rail img, .vs-rail-empty { width:44px; height:33px; border-radius:var(--radius-tile); object-fit:cover; flex-shrink:0; }
.vs-rail-empty { display:grid; place-items:center; border:1px dashed var(--border-muted); background:var(--surface-subtle); }
.vs-rail.is-empty { border-style:dashed; }
.vs-rail-text b { display:block; font-size:var(--font-kicker); font-weight:600; }
.vs-rail-text small { display:inline-flex; align-items:center; gap:3px; font-size:var(--font-kicker); color:var(--text-muted); }
.vs-rail-arrow { color:var(--text-muted); font-size:var(--font-control); }
.vs-rail-note { font-size:var(--font-kicker); color:var(--text-muted); }

/* 播放器：4:3 平板 */
.vs-player { position:relative; width:min(100%, 616px); aspect-ratio:4 / 3; overflow:hidden;
  border-radius:var(--radius-panel); background:#0b0d10; box-shadow:0 14px 44px rgba(10,12,16,.22), 0 2px 6px rgba(10,12,16,.16); }
.vs-frame { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }
.vs-player.is-demo .vs-frame-base { filter:brightness(.5) saturate(.6) blur(2px); transform:scale(1.01); }
.vs-reveal { position:absolute; inset:0;
  -webkit-mask-image:linear-gradient(90deg, #000 0, #000 calc(var(--vp) - 9%), transparent var(--vp));
  mask-image:linear-gradient(90deg, #000 0, #000 calc(var(--vp) - 9%), transparent var(--vp)); }
.vs-glow { position:absolute; top:0; bottom:0; width:110px; left:calc(var(--vp) - 110px); pointer-events:none;
  background:linear-gradient(270deg, rgba(168,196,255,.16), transparent); }
.vs-band { position:absolute; top:0; bottom:0; left:var(--vp); width:2px;
  background:linear-gradient(180deg, transparent, #dbe6ff 12%, #fff 50%, #dbe6ff 88%, transparent);
  box-shadow:0 0 16px 2px rgba(150,180,255,.55); }
.vs-tlabel { position:absolute; top:50%; left:var(--vp); transform:translate(-50%,-50%);
  padding:2px 8px; border-radius:var(--radius-pill); background:rgba(10,12,16,.62);
  box-shadow:0 0 0 1px rgba(255,255,255,.18); color:#fff; font-size:var(--font-kicker); font-variant-numeric:tabular-nums; }
.vs-empty { position:absolute; inset:0; display:grid; place-items:center; margin:0; padding:var(--space-6);
  color:rgba(255,255,255,.66); font-size:var(--font-control); text-align:center; }

/* 会话生成卡 */
.vs-card { position:absolute; left:12px; right:12px; bottom:12px; display:flex; gap:var(--space-2); }
.vs-avatar { flex:0 0 auto; width:24px; height:24px; margin-top:6px; display:grid; place-items:center;
  border-radius:var(--radius-control); background:#1f2329; color:#fff; font-size:10px; font-weight:700;
  box-shadow:0 0 0 1px rgba(255,255,255,.14); }
.vs-bubble { position:relative; flex:1; min-width:0; padding:var(--space-2) var(--space-3) var(--space-3); overflow:hidden;
  border-radius:var(--radius-panel); background:rgba(18,21,26,.88);
  box-shadow:0 0 0 1px rgba(255,255,255,.1), 0 10px 26px rgba(0,0,0,.4); }
.vs-bubble::after { content:""; position:absolute; top:0; height:1.5px; width:44%; left:-44%;
  background:linear-gradient(90deg, transparent, #9fb8ff, #fff, #9fb8ff, transparent);
  animation:vs-sweep 2.3s cubic-bezier(.4,0,.6,1) infinite; }
.vs-bubble-row { display:flex; align-items:flex-start; gap:var(--space-3); }
.vs-thumb { flex:0 0 auto; width:62px; height:47px; border-radius:var(--radius-tile); overflow:hidden; background:#191d24; }
.vs-thumb img { width:100%; height:100%; object-fit:cover; }
.vs-bubble-text { min-width:0; flex:1; }
.vs-bubble-title { display:flex; align-items:center; gap:6px; color:#fff; font-size:var(--font-control); font-weight:600; }
.vs-bubble-title em { font-style:normal; padding:1px 6px; border-radius:var(--radius-pill);
  background:rgba(127,156,255,.16); color:#a9bfff; font-size:10px; }
.vs-bubble-stage { margin-top:2px; font-size:var(--font-kicker); color:rgba(255,255,255,.62); }
.vs-bubble-stage s { text-decoration:none;
  background:linear-gradient(90deg, rgba(255,255,255,.45) 0%, rgba(255,255,255,.45) 38%, #fff 50%, rgba(255,255,255,.45) 62%, rgba(255,255,255,.45) 100%);
  background-size:300% 100%; -webkit-background-clip:text; background-clip:text; color:transparent;
  animation:vs-shimmer 2.4s linear infinite; }
.vs-track { margin-top:7px; height:3px; border-radius:var(--radius-pill); background:rgba(255,255,255,.12); overflow:hidden; }
.vs-track i { display:block; height:100%; border-radius:inherit; background:linear-gradient(90deg, #7f9cff, #b48cff); }
.vs-bubble-foot { display:flex; align-items:center; gap:var(--space-2); margin-top:6px;
  color:rgba(255,255,255,.42); font-size:10px; font-variant-numeric:tabular-nums; }
.vs-bubble-right { margin-left:auto; color:rgba(255,255,255,.72); }
.vs-done-tag { position:absolute; top:10px; left:12px; padding:2px 8px; border-radius:var(--radius-pill);
  background:rgba(39,132,92,.9); color:#fff; font-size:var(--font-kicker); font-weight:600; }

/* 操作与队列 */
.vs-actions { display:flex; align-items:center; gap:var(--space-2); flex-wrap:wrap; }
.vs-actions-note { display:inline-flex; align-items:center; gap:4px; margin-left:auto; font-size:var(--font-kicker); color:var(--text-muted); }
.vs-queue { display:flex; align-items:center; gap:var(--space-2); padding:var(--space-2) 0;
  font-size:var(--font-kicker); color:var(--text-muted); }
.vs-dot { width:6px; height:6px; border-radius:50%; background:var(--border-strong, #c8ccd2); flex-shrink:0; }
.vs-dot.is-live { background:var(--accent); box-shadow:0 0 0 3px var(--sidebar-selected); }
.vs-queue-right { margin-left:auto; }

/* 参数面板 */
.vs-side { display:flex; flex-direction:column; gap:var(--space-3); padding:var(--space-4);
  border:1px solid var(--border-muted); border-radius:var(--radius-panel); background:var(--surface); }
.vs-side-head { display:flex; align-items:baseline; gap:var(--space-2); }
.vs-side-head b { font-size:var(--font-control); }
.vs-side-head small { margin-left:auto; font-size:var(--font-kicker); color:var(--text-muted); }
.vs-field { display:grid; gap:6px; font-size:var(--font-kicker); color:var(--text-muted); }
.vs-field select, .vs-field input { width:100%; min-height:var(--control-sm); padding:0 var(--space-2);
  border:1px solid var(--border-muted); border-radius:var(--radius-control); background:var(--surface);
  color:var(--text-primary); font:inherit; font-size:var(--font-control); }
.vs-field input:disabled, .vs-field select:disabled { background:var(--surface-subtle); color:var(--text-muted); }
.vs-seg { display:flex; gap:var(--space-1); }
.vs-seg-wrap { flex-wrap:wrap; }
.vs-seg button { flex:1 1 auto; min-width:52px; min-height:var(--control-sm); padding:0 var(--space-2);
  border:1px solid var(--border-muted); border-radius:var(--radius-control); background:var(--surface);
  color:var(--text-secondary); font:inherit; font-size:var(--font-control); cursor:pointer; }
.vs-seg button[aria-pressed="true"] { background:var(--sidebar-selected); color:var(--text-primary); border-color:var(--border-strong, #c8ccd2); font-weight:500; }
.vs-seg button:disabled { opacity:.6; cursor:not-allowed; }
.vs-facts { display:grid; gap:6px; margin:0; padding-top:var(--space-3); border-top:1px solid var(--border-muted); }
.vs-facts div { display:grid; grid-template-columns:60px minmax(0,1fr); gap:var(--space-2); font-size:var(--font-kicker); }
.vs-facts dt { color:var(--text-muted); }
.vs-facts dd { margin:0; color:var(--text-secondary); }
.vs-side-note { margin:0; font-size:var(--font-kicker); line-height:1.6; color:var(--text-muted); }

@keyframes vs-sweep { from { left:-44%; } to { left:100%; } }
@keyframes vs-shimmer { from { background-position:100% 0; } to { background-position:-100% 0; } }

@media (max-width:1180px) {
  .vs-grid { grid-template-columns:minmax(0, 1fr); }
  .vs-side { order:2; }
}
@media (prefers-reduced-motion:reduce) {
  .vs-bubble::after { animation:none; opacity:0; }
  .vs-bubble-stage s { animation:none; background:none; -webkit-text-fill-color:currentColor; color:rgba(255,255,255,.72); }
}
</style>
