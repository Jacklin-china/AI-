<script setup lang="ts">
import { ref } from 'vue'

import type { StudioTask } from '../types'
import StatusBadge from './StatusBadge.vue'

defineProps<{ task: StudioTask | null; busy: boolean }>()
const emit = defineEmits<{
  precheck: []
  decide: [decision: 'approve' | 'reject' | 'request_revision', reason: string, notes: string]
  archive: []
}>()
const reason = ref('composition')
const notes = ref('')
</script>

<template>
  <section class="subpanel approval-panel">
    <header><div><span class="section-kicker">审批</span><h3>人工确认</h3></div><StatusBadge :status="task?.archived ? 'completed' : task?.review ? (task.review.approved ? 'completed' : 'failed') : task?.qc ? 'waiting' : 'pending'" /></header>
    <div v-if="!task?.has_image" class="panel-placeholder">生成图片后，审批操作会在这里出现。</div>
    <button v-else-if="!task.qc" class="ui-button primary full" :disabled="busy" @click="emit('precheck')">运行视觉预筛</button>
    <div v-else-if="!task.review" class="approval-form"><label>不通过原因<select v-model="reason" class="ui-select"><option value="composition">构图问题</option><option value="persona_drift">人物漂移</option><option value="broken_hands">手部错误</option><option value="watermark">水印</option><option value="cinematography">摄影语言</option><option value="ai_artifact">AI 痕迹</option><option value="audience_mismatch">受众不匹配</option><option value="other">其他</option></select></label><label>人工备注<input v-model="notes" class="ui-input" placeholder="可选；默认沿用 QC 理由" /></label><div class="approval-buttons"><button class="ui-button danger" :disabled="busy" @click="emit('decide', 'reject', reason, notes)">拒绝</button><button class="ui-button" :disabled="busy" @click="emit('decide', 'request_revision', reason, notes)">要求修改</button><button class="ui-button primary" :disabled="busy" @click="emit('decide', 'approve', reason, notes)">批准</button></div></div>
    <div v-else class="approval-result"><p>{{ task.review.approved ? '人工已批准，可归档交付。' : task.rework?.status === 'pending' ? '已进入返修队列，尚未产生新费用。' : '作品已拒绝，返修项已关闭。' }}</p><button class="ui-button primary full" :disabled="busy || task.archived" @click="emit('archive')">{{ task.archived ? '已归档' : '归档 Artifact' }}</button></div>
  </section>
</template>
