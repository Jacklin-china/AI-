<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'

import { getState, loadArtwork, startAction } from './api'
import type { StudioForm, StudioState, StudioTask } from './types'

type ViewName = 'create' | 'archive'
type NoticeTone = 'normal' | 'error' | 'success'

const emptyState: StudioState = {
  tasks: [],
  job: { state: 'idle' },
  config: { image: '—', chat: '—', vision: '—', size: '—', limit: '20' },
}
const defaultForm: StudioForm = {
  project: '我的第一份作品',
  shot_no: 1,
  purpose: '叙事静帧',
  subject: '',
  style: '自然光、克制色彩、纪实摄影',
  audience: '喜欢自然、有生活气息画面的用户',
  prompt: '',
  price: '3.00',
}

const state = ref<StudioState>(emptyState)
const form = reactive<StudioForm>({ ...defaultForm })
const activeView = ref<ViewName>('create')
const selectedId = ref<string | null>(null)
const artworkUrl = ref<string | null>(null)
const noticeText = ref('工作台已就绪。打开页面不会调用模型。')
const noticeTone = ref<NoticeTone>('normal')
const loadingImage = ref(false)
const firstRefresh = ref(true)
const lastJobId = ref('')
const refreshing = ref(false)
let timer: number | undefined

const modal = reactive({
  open: false,
  title: '',
  message: '',
  confirmLabel: '确认继续',
  showPrice: false,
  price: '',
  cancelLabel: '返回',
})
let resolveModal: ((result: boolean) => void) | null = null

const selected = computed<StudioTask | null>(() =>
  state.value.tasks.find((task) => task.request_id === selectedId.value) ?? null,
)
const busy = computed(() => state.value.job.state === 'running')
const completedCount = computed(
  () => state.value.tasks.filter((task) => task.status === 'succeeded').length,
)
const waitingCount = computed(
  () => state.value.tasks.filter((task) => task.status === 'unknown').length,
)
const promptLength = computed(() => form.prompt.length)
const statusLabel = (status: StudioTask['status']): string =>
  ({ succeeded: '已生成', failed: '失败', unknown: '待核对', draft: '草稿' })[status]

function setNotice(text: string, tone: NoticeTone = 'normal'): void {
  noticeText.value = text
  noticeTone.value = tone
}

function payload(): Record<string, unknown> {
  return {
    project: form.project.trim(),
    shot_no: Number(form.shot_no),
    purpose: form.purpose,
    subject: form.subject,
    style: form.style,
    audience: form.audience,
    prompt: form.prompt,
    price: form.price,
    request_id: selected.value?.request_id,
    confirmed: true,
  }
}

function saveDraft(): void {
  try {
    localStorage.setItem('kantoku-vue-draft', JSON.stringify(form))
  } catch {
    // 浏览器禁用本地存储时不影响主要创作流程。
  }
}

function restoreDraft(): void {
  try {
    const saved = JSON.parse(localStorage.getItem('kantoku-vue-draft') ?? '{}') as Partial<StudioForm>
    Object.assign(form, { ...defaultForm, ...saved })
  } catch {
    Object.assign(form, defaultForm)
  }
}

function ask(
  title: string,
  message: string,
  options: { showPrice?: boolean; confirmLabel?: string; cancelLabel?: string } = {},
): Promise<boolean> {
  modal.title = title
  modal.message = message
  modal.showPrice = options.showPrice ?? false
  modal.confirmLabel = options.confirmLabel ?? '确认继续'
  modal.cancelLabel = options.cancelLabel ?? '返回'
  modal.price = ''
  modal.open = true
  return new Promise((resolve) => {
    resolveModal = resolve
  })
}

function closeModal(result: boolean): void {
  modal.open = false
  resolveModal?.(result)
  resolveModal = null
}

async function run(action: string, body: Record<string, unknown> = payload()): Promise<void> {
  if (busy.value) return
  setNotice('正在处理。任务记录和预算保护会一直保留。')
  try {
    await startAction(action, body)
    await refresh()
  } catch (error) {
    setNotice(error instanceof Error ? error.message : '操作没有完成', 'error')
  }
}

async function selectTask(task: StudioTask): Promise<void> {
  selectedId.value = task.request_id
  form.project = task.project
  form.shot_no = task.shot_no
  form.prompt = task.prompt
  activeView.value = 'create'
  saveDraft()
  if (artworkUrl.value) {
    URL.revokeObjectURL(artworkUrl.value)
    artworkUrl.value = null
  }
  if (task.has_image) {
    loadingImage.value = true
    try {
      artworkUrl.value = await loadArtwork(task.request_id)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '图片读取失败', 'error')
    } finally {
      loadingImage.value = false
    }
  }
  setNotice(task.error ?? '已载入历史画面；继续时会沿用原任务记录。')
}

async function handleCompletedJob(snapshot: StudioState): Promise<void> {
  const job = snapshot.job
  if (!job.id || job.id === lastJobId.value || job.state === 'running') return
  lastJobId.value = job.id
  if (job.state === 'error') {
    setNotice(job.error ?? '操作没有完成', 'error')
    return
  }
  const result = job.result ?? {}
  if (result.prompt) {
    form.prompt = result.prompt
    saveDraft()
    setNotice('提示词已更新。请检查主体、动作和光线后再生成。', 'success')
  }
  if (result.request_id) {
    const task = snapshot.tasks.find((item) => item.request_id === result.request_id)
    if (task) await selectTask(task)
  }
  if (result.qc) {
    setNotice(
      `${result.qc.reason} · 模型置信度 ${Math.round(result.qc.confidence * 100)}%，最终仍由你判断。`,
      'success',
    )
  }
  if (result.message) setNotice(result.message, 'success')
  if (job.action === 'budget' && result.available_fen !== undefined) {
    setNotice(
      `已结算 ¥${((result.settled_fen ?? 0) / 100).toFixed(2)} · 预占 ¥${((result.held_fen ?? 0) / 100).toFixed(2)} · 可用 ¥${(result.available_fen / 100).toFixed(2)}`,
      'success',
    )
  }
}

async function refresh(): Promise<void> {
  if (refreshing.value) return
  refreshing.value = true
  try {
    const snapshot = await getState()
    state.value = snapshot
    if (firstRefresh.value) {
      lastJobId.value = snapshot.job.state === 'running' ? '' : snapshot.job.id ?? ''
      firstRefresh.value = false
    }
    await handleCompletedJob(snapshot)
  } catch (error) {
    setNotice(
      `连接暂不可用：${error instanceof Error ? error.message : '请确认启动窗口仍在运行'}`,
      'error',
    )
  } finally {
    refreshing.value = false
  }
}

async function refine(): Promise<void> {
  if (!form.prompt.trim()) {
    setNotice('请先整理提示词或填写创作内容。', 'error')
    return
  }
  if (
    await ask(
      '交给创意模型优化？',
      '当前提示词将发送给已配置的文本模型。它只改写设计方案，不会生成图片，也不会动用生图预算。',
    )
  ) {
    await run('refine')
  }
}

async function generate(): Promise<void> {
  if (
    !form.project.trim() ||
    !form.prompt.trim() ||
    !Number.isInteger(Number(form.shot_no)) ||
    Number(form.shot_no) < 1 ||
    !(Number(form.price) > 0)
  ) {
    setNotice('请填写作品名、正整数画面编号、最终提示词和费用上界。', 'error')
    return
  }
  const approved = await ask(
    '确认生成一张画面',
    `${form.project} / 画面 ${form.shot_no}\n本次最多预占 ¥${form.price}\n系统只生成一张。已有任务请使用“查询原任务”，避免重复收费。`,
    { confirmLabel: '确认并生成' },
  )
  if (approved) await run('generate')
}

async function resume(): Promise<void> {
  if (!selected.value) {
    setNotice('请先从任务轨道选择一条记录。', 'error')
    return
  }
  if (
    await ask(
      '继续原任务',
      `沿用原提示词与 ¥${(selected.value.estimate_fen / 100).toFixed(2)} 费用上界。未提交任务只会执行一次；已提交任务不会重复提交。`,
    )
  ) {
    await run('resume')
  }
}

async function recover(): Promise<void> {
  if (!selected.value) {
    setNotice('请先选择要查询的任务。', 'error')
    return
  }
  await run('recover')
}

async function qualityCheck(): Promise<void> {
  if (!selected.value?.has_image) {
    setNotice('请先选择一张已经生成的图片。', 'error')
    return
  }
  if (
    await ask(
      '启动视觉预筛？',
      '当前图片会发送给视觉模型，从动作、材质、构图与叙事四个方向给出辅助意见。结果不是最终裁决。',
    )
  ) {
    await run('qc')
  }
}

async function settleBill(): Promise<void> {
  if (!selected.value) {
    setNotice('请先选择需要对账的任务。', 'error')
    return
  }
  const approved = await ask(
    '回填供应商账单',
    `任务 ${selected.value.request_id.slice(-8)}\n只填写平台已经确认的最终人民币实扣。`,
    { showPrice: true, confirmLabel: '确认实扣' },
  )
  if (!approved) return
  if (modal.price === '' || Number(modal.price) < 0) {
    setNotice('请填写精确到分的非负金额。', 'error')
    return
  }
  await run('settle', { ...payload(), price: modal.price })
}

function downloadArtwork(): void {
  if (!artworkUrl.value || !selected.value) {
    setNotice('当前没有可下载的图片。', 'error')
    return
  }
  const link = document.createElement('a')
  link.href = artworkUrl.value
  link.download = `${selected.value.request_id}.png`
  link.click()
}

async function showSettings(): Promise<void> {
  const config = state.value.config
  await ask(
    '当前模型路由',
    `文本设计　${config.chat}\n视觉质检　${config.vision}\n图片生成　${config.image}\n输出尺寸　${config.size}\n\n密钥始终只保存在本机，不会发送给这个页面。`,
    { confirmLabel: '知道了', cancelLabel: '关闭' },
  )
}

function formatTime(raw: string): string {
  if (!raw) return '尚未提交'
  const parsed = new Date(raw.replace(' ', 'T') + 'Z')
  return Number.isNaN(parsed.valueOf())
    ? raw.slice(0, 16)
    : parsed.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

watch(form, saveDraft, { deep: true })
onMounted(() => {
  restoreDraft()
  void refresh()
  timer = window.setInterval(() => void refresh(), 1600)
})
onBeforeUnmount(() => {
  if (timer !== undefined) window.clearInterval(timer)
  if (artworkUrl.value) URL.revokeObjectURL(artworkUrl.value)
})
</script>

<template>
  <div class="app-shell">
    <aside class="rail" aria-label="主导航">
      <button class="brand" title="监督酱" @click="activeView = 'create'">
        <span class="brand-mark">K</span>
        <span class="brand-copy"><strong>监督酱</strong><small>个人创作工作台</small></span>
      </button>
      <nav>
        <button :class="{ active: activeView === 'create' }" @click="activeView = 'create'">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 17.5V20h2.5L18.4 8.1l-2.5-2.5L4 17.5ZM14.8 6.7l2.5 2.5M13 20h7" /></svg>
          <span>创作</span>
        </button>
        <button :class="{ active: activeView === 'archive' }" @click="activeView = 'archive'">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16v13H4zM3 4h18v3H3zM9 11h6" /></svg>
          <span>档案</span>
        </button>
      </nav>
      <button class="rail-settings" title="模型配置" @click="showSettings">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.4 1A8 8 0 0 0 15 6l-.3-2.6h-4L10.4 6a8 8 0 0 0-1.5.9l-2.4-1-2 3.4 2 1.5a7 7 0 0 0 0 2.2l-2 1.5 2 3.4 2.4-1a8 8 0 0 0 1.5.9l.3 2.6h4l.3-2.6a8 8 0 0 0 1.5-.9l2.4 1 2-3.4-2-1.5c.1-.3.1-.7.1-1Z" /></svg>
        <span>模型配置</span>
      </button>
    </aside>

    <div class="stage">
      <header class="topbar">
        <div class="wordmark">创作工作台</div>
        <div class="system-state"><i :class="{ running: busy }"></i>{{ busy ? '任务执行中' : '本机系统就绪' }}</div>
      </header>

      <main v-if="activeView === 'create'">
        <section class="hero">
          <div>
            <h1>开始创作</h1>
            <p class="hero-copy">把模糊想法整理成可交付画面。先明确设计，再生成一张。</p>
          </div>
          <div class="budget-block">
            <span>单项目生图上限</span>
            <strong><small>¥</small>{{ state.config.limit }}</strong>
            <b>01 IMAGE / REQUEST</b>
          </div>
        </section>

        <section class="progress-strip" aria-label="创作流程">
          <div class="active"><b>01</b><span>定义画面</span></div><i></i>
          <div><b>02</b><span>完善提示词</span></div><i></i>
          <div><b>03</b><span>确认费用</span></div><i></i>
          <div><b>04</b><span>审片交付</span></div>
          <small>单张优先 · 禁止自动返工</small>
        </section>

        <section class="workspace">
          <aside class="brief-panel panel">
            <div class="panel-heading">
              <div><span>INPUT / BRIEF</span><h2>创作定义</h2></div>
              <b>01</b>
            </div>
            <div class="form-grid">
              <label class="wide">作品名称<input v-model.trim="form.project" :disabled="busy" /></label>
              <label>画面编号<input v-model.number="form.shot_no" type="number" min="1" :disabled="busy" /></label>
              <label>交付用途
                <select v-model="form.purpose" :disabled="busy">
                  <option>叙事静帧</option><option>宣传海报</option><option>产品主视觉</option><option>社交媒体配图</option>
                </select>
              </label>
              <label class="full">想呈现什么
                <textarea v-model="form.subject" rows="4" placeholder="先写主体、动作、环境和希望观众感受到什么。" :disabled="busy"></textarea>
              </label>
              <label class="full">目标受众<input v-model="form.audience" :disabled="busy" /></label>
              <label class="full">视觉方向<input v-model="form.style" :disabled="busy" /></label>
            </div>
            <button class="action outline" :disabled="busy" @click="run('compose')">
              <span>整理为提示词</span><b>FREE</b>
            </button>

            <div class="prompt-section">
              <div class="subheading"><span>FINAL PROMPT</span><b>{{ promptLength }} CHARS</b></div>
              <textarea v-model="form.prompt" rows="9" placeholder="整理后的提示词会出现在这里，你可以继续修改。" :disabled="busy"></textarea>
              <button class="action dark" :disabled="busy" @click="refine">
                <span>让创意模型优化</span><b>GPT‑5.6 →</b>
              </button>
            </div>

            <div class="cost-control">
              <label>本次费用上界 / 元<input v-model="form.price" type="number" min="0.01" step="0.01" :disabled="busy" /></label>
              <p>输入你在平台核实的单次上界。系统会先预占，未知账单不会被当作免费。</p>
            </div>
            <button class="generate" :disabled="busy" @click="generate">
              <span>{{ busy ? '任务处理中' : '确认费用并生成一张' }}</span><b>↗</b>
            </button>
          </aside>

          <section class="output-column">
            <article class="canvas-panel panel">
              <div class="panel-heading compact">
                <div><span>OUTPUT / FRAME</span><h2>{{ selected ? `${selected.project} · 画面 ${selected.shot_no}` : '等待第一张画面' }}</h2></div>
                <span class="status" :data-status="selected?.status ?? 'draft'">{{ selected ? statusLabel(selected.status) : '未提交' }}</span>
              </div>
              <div class="canvas">
                <img v-if="artworkUrl" :src="artworkUrl" alt="生成的画面" />
                <div v-else class="canvas-empty">
                  <div class="target-mark"><i></i><i></i></div>
                  <b>{{ loadingImage ? '正在读取画面' : '画面将在这里出现' }}</b>
                  <p>先把主体与动作说明白，再花第一次钱。</p>
                </div>
                <span class="coordinate top">KNT / VIEWPORT</span>
                <span class="coordinate bottom">{{ state.config.size }}</span>
              </div>
              <div class="canvas-actions">
                <button :disabled="busy || !selected" @click="recover">查询原任务</button>
                <button :disabled="busy || !selected" @click="resume">继续未提交任务</button>
                <button :disabled="!artworkUrl" @click="downloadArtwork">下载原图</button>
              </div>
            </article>

            <div class="insight-grid">
              <article class="review-panel panel">
                <div class="panel-heading compact">
                  <div><span>QUALITY GATE</span><h2>审片判断</h2></div>
                  <button class="link-button" :disabled="busy || !selected?.has_image" @click="qualityCheck">视觉预筛 ↗</button>
                </div>
                <div class="criteria"><span>动作关系</span><span>人物情绪</span><span>材质光线</span><span>叙事焦点</span></div>
                <p>先看人物是否真的在做这件事，再看表情、接触点、衣物材质与环境因果。模型负责预筛，你负责最终通过。</p>
                <footer>生成成功 ≠ 可交付</footer>
              </article>
              <article class="model-panel panel">
                <div class="panel-heading compact"><div><span>MODEL ROUTE</span><h2>模型分工</h2></div></div>
                <dl><div><dt>创意 / 视觉</dt><dd>{{ state.config.chat }}</dd></div><div><dt>图片生成</dt><dd>{{ state.config.image }}</dd></div></dl>
                <button class="link-button" @click="showSettings">查看完整配置 ↗</button>
              </article>
            </div>

            <article class="history-panel">
              <div class="section-heading"><div><span>RECENT FRAMES</span><h2>任务轨道</h2></div><b>{{ state.tasks.length.toString().padStart(2, '0') }} RECORDS</b></div>
              <div v-if="state.tasks.length" class="task-track">
                <button v-for="task in state.tasks" :key="task.request_id" :class="{ selected: task.request_id === selectedId }" @click="selectTask(task)">
                  <i :data-status="task.status"></i><span>{{ task.project }}</span><strong>画面 {{ task.shot_no.toString().padStart(2, '0') }}</strong><small>{{ statusLabel(task.status) }} · {{ formatTime(task.created_at) }}</small>
                </button>
              </div>
              <div v-else class="empty-track">没有历史任务。完成一次生成后，原任务会留在这里供查询与对账。</div>
            </article>
          </section>
        </section>
      </main>

      <main v-else class="archive-view">
        <section class="archive-header">
          <div><p class="kicker">LOCAL PRODUCTION ARCHIVE</p><h1>作品档案。</h1><p>全部记录保存在本机；从这里回到原任务，不另建重复请求。</p></div>
          <div class="archive-stats"><div><strong>{{ state.tasks.length }}</strong><span>全部记录</span></div><div><strong>{{ completedCount }}</strong><span>已有画面</span></div><div><strong>{{ waitingCount }}</strong><span>待核对</span></div></div>
        </section>
        <section v-if="state.tasks.length" class="archive-grid">
          <button v-for="(task, index) in state.tasks" :key="task.request_id" @click="selectTask(task)">
            <span class="index">{{ String(index + 1).padStart(2, '0') }}</span><div><small>{{ statusLabel(task.status) }} / {{ task.request_id.slice(-8) }}</small><h2>{{ task.project }}</h2><p>画面 {{ task.shot_no }} · 上界 ¥{{ (task.estimate_fen / 100).toFixed(2) }}</p></div><b>↗</b>
          </button>
        </section>
        <div v-else class="archive-empty">当前还没有创作记录。</div>
      </main>

      <div class="notice" :data-tone="noticeTone" role="status" aria-live="polite"><i></i><span>{{ noticeText }}</span><button v-if="form.project" :disabled="busy" @click="run('budget')">预算概览</button><button v-if="selected" :disabled="busy" @click="settleBill">账单回填</button></div>
      <footer class="global-footer"><span>KANTOKU / PERSONAL CREATIVE SYSTEM</span><span>LOCALHOST · PRIVATE WORKSPACE</span></footer>
    </div>

    <div v-if="modal.open" class="modal-backdrop" @click.self="closeModal(false)">
      <section class="modal" role="dialog" aria-modal="true" :aria-label="modal.title">
        <span>OPERATION CONFIRMATION</span><h2>{{ modal.title }}</h2><p>{{ modal.message }}</p>
        <label v-if="modal.showPrice">平台最终实扣 / 元<input v-model="modal.price" type="number" min="0" step="0.01" autofocus /></label>
        <div><button class="modal-cancel" @click="closeModal(false)">{{ modal.cancelLabel }}</button><button class="modal-confirm" @click="closeModal(true)">{{ modal.confirmLabel }}</button></div>
      </section>
    </div>
  </div>
</template>
