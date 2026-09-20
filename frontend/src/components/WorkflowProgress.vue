<script setup lang="ts">
import { AlertCircle, Check, Loader2, Minus } from 'lucide-vue-next'

export type WorkflowStepStatus = 'pending' | 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled'
export interface WorkflowStep { id: string; name: string; status: WorkflowStepStatus; detail?: string }
defineProps<{ steps: WorkflowStep[]; compact?: boolean }>()
</script>

<template>
  <ol class="workflow-progress" :class="{ compact }">
    <li v-for="(step, index) in steps" :key="step.id" :data-status="step.status">
      <div class="step-rail"><span><Check v-if="step.status === 'completed'" :size="11" :stroke-width="3" /><Loader2 v-else-if="step.status === 'running'" :size="11" :stroke-width="2.5" class="spin" /><AlertCircle v-else-if="step.status === 'failed'" :size="11" :stroke-width="2.5" /><Minus v-else-if="step.status === 'waiting'" :size="11" :stroke-width="2.5" /><template v-else>{{ index + 1 }}</template></span><i v-if="index < steps.length - 1"></i></div>
      <div><strong>{{ step.name }}</strong><small>{{ step.detail ?? step.status }}</small></div>
    </li>
  </ol>
</template>
