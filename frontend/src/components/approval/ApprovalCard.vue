<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { CoreApproval } from '../../types'
import { presenterFor } from '../../domains/presenters'

interface Candidate { id: string; title: string; cost_fen: number; skus: string[]; source: string }

const props = defineProps<{ approval: CoreApproval; domain: string; busy: boolean }>()
const emit = defineEmits<{ decide: [action: 'approve' | 'reject' | 'revise', response: Record<string, unknown>] }>()

const selected = ref<string>('')
const reviseNote = ref('')
const showRaw = ref(false)
const submitting = ref<'approve' | 'reject' | 'revise' | null>(null)

const kind = computed(() => String(props.approval.request?.kind ?? ''))
const presenter = computed(() => presenterFor(props.domain))
const resolved = computed(() => props.approval.decision !== 'pending')
const candidates = computed<Candidate[]>(() => (props.approval.request?.candidates ?? []) as Candidate[])
const listing = computed(() => (props.approval.request?.listing ?? {}) as Record<string, unknown>)
const qc = computed(() => (props.approval.request?.qc ?? null) as Record<string, unknown> | null)
const image = computed(() => (props.approval.request?.image ?? null) as Record<string, unknown> | null)
const isMock = computed(() => props.approval.request?.origin === 'mock' || Boolean(props.approval.request?.mock))
const title = computed(() => presenter.value.approvalTitle(kind.value || null))

const decisionLabel: Record<string, string> = {
  approve: '已批准',
  reject: '已拒绝',
  request_revision: '已要求修改',
}

function money(fen: number): string {
  return `¥${(fen / 100).toFixed(2)}`
}

function submit(action: 'approve' | 'reject' | 'revise'): void {
  if (resolved.value || submitting.value) return
  submitting.value = action
  const response: Record<string, unknown> = {}
  if (action === 'approve' && kind.value === 'candidate_approval') {
    response.candidate_id = selected.value || candidates.value[0]?.id || null
  }
  if (action === 'revise') response.revision_instruction = reviseNote.value.trim()
  emit('decide', action, response)
}

defineExpose({ resetSubmit: () => { submitting.value = null } })
watch(() => props.busy, (value) => { if (!value) submitting.value = null })
</script>

<template>
  <section class="approval-card" :data-resolved="resolved" :aria-busy="busy || submitting !== null">
    <header>
      <div>
        <span class="section-kicker">需要你决定</span>
        <h3>{{ title }}</h3>
      </div>
      <span v-if="isMock" class="mock-label">Mock</span>
    </header>

    <div v-if="kind === 'candidate_approval' && candidates.length" class="approval-options" role="radiogroup" aria-label="候选商品">
      <label v-for="item in candidates" :key="item.id" :class="{ picked: selected === item.id }">
        <input v-model="selected" type="radio" name="candidate" :value="item.id" :disabled="resolved" />
        <span><b>{{ item.title }}</b><small>{{ money(item.cost_fen) }} · {{ item.skus.length }} SKU · {{ item.source }}</small></span>
      </label>
    </div>

    <div v-else-if="kind === 'publish_approval'" class="publish-summary">
      <div class="publish-row"><span>商品</span><strong>{{ String(listing.title ?? '—') }}</strong></div>
      <div class="publish-row"><span>售价</span><strong>{{ listing.price_fen ? money(Number(listing.price_fen)) : '—' }}</strong></div>
      <div class="publish-row"><span>Locale</span><strong>{{ String(listing.locale ?? '—') }}</strong></div>
      <div class="publish-row" v-if="qc"><span>QC</span><strong>{{ qc.passed ? '通过' : '未通过' }}</strong></div>
      <div class="publish-row" v-if="image"><span>商品主图</span><strong>{{ image.origin === 'real' ? 'Real · 已生成' : image.origin === 'blocked' ? 'Blocked' : 'Mock · 未生成真实图片' }}</strong></div>
      <div class="publish-row"><span>Marketplace</span><strong>Mock Marketplace</strong></div>
      <p class="publish-desc">{{ String(listing.description ?? '') }}</p>
    </div>

    <div v-else-if="kind === 'cost_approval'" class="cost-approval-summary">
      <div><span>预计费用</span><strong>{{ money(Number(approval.request.estimate_fen ?? 0)) }}</strong></div>
      <div><span>服务</span><strong>{{ String(approval.request.provider ?? '即梦') }}</strong></div>
      <p>{{ String(approval.request.message ?? '批准后才会调用付费服务。') }}</p>
    </div>

    <div v-else class="approval-generic">
      <dl>
        <div v-for="(value, key) in approval.request" :key="String(key)">
          <dt>{{ String(key) }}</dt>
          <dd>{{ typeof value === 'object' ? '（结构化数据，见原始数据）' : String(value) }}</dd>
        </div>
      </dl>
    </div>

    <label v-if="!resolved" class="revise-field">
      <span>希望修改什么？（可选，用于「要求修改」）</span>
      <input v-model="reviseNote" class="ui-input" type="text" placeholder="例如：优先选择采购成本低于 ¥15 的商品" />
    </label>

    <footer>
      <template v-if="resolved">
        <span class="decision-chip">已处理 · {{ decisionLabel[approval.decision] ?? approval.decision }}</span>
      </template>
      <template v-else>
        <button class="ui-button sm" :disabled="busy || submitting !== null" @click="submit('reject')">拒绝</button>
        <button class="ui-button sm" :disabled="busy || submitting !== null" @click="submit('revise')">要求修改</button>
        <button class="ui-button sm primary" :disabled="busy || submitting !== null" @click="submit('approve')">
          {{ submitting === 'approve' ? '提交中……' : '批准并继续' }}
        </button>
      </template>
      <button class="text-action" @click="showRaw = !showRaw">{{ showRaw ? '隐藏' : '查看' }}原始数据</button>
    </footer>

    <pre v-if="showRaw" class="raw-json">{{ JSON.stringify(approval.request, null, 2) }}</pre>
  </section>
</template>
