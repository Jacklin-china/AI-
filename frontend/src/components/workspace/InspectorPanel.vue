<script setup lang="ts">
import { ref } from 'vue'

import type { GenerationSettings as GenerationSettingsType, StudioForm, StudioTask } from '../../types'
import ApprovalPanel from '../ApprovalPanel.vue'
import QcPanel from '../QcPanel.vue'
import StatusBadge from '../StatusBadge.vue'
import GenerationSettings from './GenerationSettings.vue'

defineProps<{ form: StudioForm; settings: GenerationSettingsType; task: StudioTask | null; busy: boolean; imageModel: string; imageSize: string; estimate: string; artworkUrl?: string }>()
const emit = defineEmits<{ precheck: []; decide: [decision: 'approve' | 'reject' | 'request_revision', reason: string, notes: string]; archive: [] }>()
const tab = ref<'context' | 'settings' | 'qc' | 'artifacts'>('context')
</script>

<template>
  <aside class="inspector-panel">
    <div class="inspector-tabs"><button v-for="item in ([['context','上下文'],['settings','设置'],['qc','质检'],['artifacts','产物']] as const)" :key="item[0]" :class="{ active: tab === item[0] }" @click="tab = item[0]">{{ item[1] }}</button></div>
    <section v-if="tab === 'context'" class="inspector-content"><header class="panel-header"><div><span class="section-kicker">上下文</span><h2>当前任务</h2></div></header><dl class="context-list"><div><dt>项目</dt><dd>{{ form.project }}</dd></div><div><dt>用途</dt><dd>{{ form.purpose }}</dd></div><div><dt>受众</dt><dd>{{ form.audience }}</dd></div><div><dt>风格</dt><dd>{{ form.style }}</dd></div></dl></section>
    <section v-else-if="tab === 'settings'" class="inspector-content"><header class="panel-header"><div><span class="section-kicker">参数</span><h2>生成设置</h2></div></header><GenerationSettings :settings="settings" :image-model="imageModel" :image-size="imageSize" :estimate="estimate" /></section>
    <section v-else-if="tab === 'qc'" class="inspector-content flush"><QcPanel :qc="task?.qc ?? null" /><ApprovalPanel :task="task" :busy="busy" @precheck="emit('precheck')" @decide="(decision, reason, notes) => emit('decide', decision, reason, notes)" @archive="emit('archive')" /></section>
    <section v-else class="inspector-content"><header class="panel-header"><div><span class="section-kicker">产物</span><h2>当前产物</h2></div><StatusBadge :status="task?.has_image ? 'completed' : 'pending'" /></header><div class="inspector-artifact"><img v-if="artworkUrl" :src="artworkUrl" alt="Artifact" /><span v-else>尚无 Artifact</span></div><p class="inspector-note">这里只展示当前 Run 的真实产物和来源状态。</p></section>
  </aside>
</template>
