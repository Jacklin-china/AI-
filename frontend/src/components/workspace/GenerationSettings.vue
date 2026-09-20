<script setup lang="ts">
import type { GenerationSettings } from '../../types'

defineProps<{ settings: GenerationSettings; imageModel: string; imageSize: string; estimate: string }>()
</script>

<template>
  <div class="stack-form">
    <label>Generation Type<div class="segmented"><button class="active">Image</button><button disabled>Video · Preview</button></div></label>
    <label>Model<select v-model="settings.model" class="ui-select"><option :value="imageModel">{{ imageModel }}</option></select></label>
    <label>Aspect Ratio<div class="segmented compact"><button v-for="ratio in ['1:1', '3:4', '16:9', '9:16']" :key="ratio" :class="{ active: settings.ratio === ratio }" @click="settings.ratio = ratio">{{ ratio }}</button></div></label>
    <div class="two-fields"><label>Resolution<select v-model="settings.resolution" class="ui-select"><option>1K</option><option>2K</option><option>4K</option></select></label><label>Quality<select v-model="settings.quality" class="ui-select"><option value="draft">Draft</option><option value="standard">Standard</option><option value="high">High</option></select></label></div>
    <label>Quantity<select v-model.number="settings.quantity" class="ui-select"><option :value="1">1 · 当前真实接口</option></select></label>
    <details class="advanced-settings"><summary>高级设置</summary><div><label>种子<input class="ui-input" value="自动" disabled /></label><label>调度器<input class="ui-input" value="供应商默认" disabled /></label><p>高级参数尚未由当前 Provider Adapter 暴露，因此保持只读。</p></div></details>
  </div>
  <div class="model-note"><span>MODEL ROUTE</span><b>{{ imageModel }}</b><small>{{ imageSize }} · 单图预算预占 {{ estimate }}</small></div>
</template>
