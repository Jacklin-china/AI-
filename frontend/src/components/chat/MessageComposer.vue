<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { ArrowUp, Paperclip, Plus, X } from 'lucide-vue-next'
type FastDomain = 'comic'
const props = defineProps<{ disabled?: boolean; fastDomains?: boolean; fastDomain?: FastDomain | null; fastDomainBusy?: boolean }>()
const emit = defineEmits<{ send: [content: string]; selectFastDomain: [domain: FastDomain | null] }>()
const value = ref('')
const input = ref<HTMLTextAreaElement | null>(null)
const menuOpen = ref(false)
/* 创作域只保留漫剧：快捷模式菜单不再提供电商/通用创作（后端实现保留，仅前端入口收敛）。 */
const domainNames: Record<FastDomain, string> = { comic: '漫剧创作' }
function selectDomain(domain: FastDomain | null): void { menuOpen.value = false; emit('selectFastDomain', domain); input.value?.focus() }
const canSend = computed(() => value.value.trim() !== '' && !props.disabled)
function submit(): void { if (!canSend.value) return; const content = value.value.trim(); value.value = ''; emit('send', content); void nextTick(resize) }
function keydown(event: KeyboardEvent): void { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submit() } }
function resize(): void { const el = input.value; if (!el) return; el.style.height = '0'; el.style.height = `${Math.min(el.scrollHeight, 168)}px` }
function fill(text: string): void { value.value = text; void nextTick(() => { resize(); input.value?.focus() }) }
defineExpose({ fill })
</script>
<template>
  <div class="message-composer" @keydown.esc="menuOpen = false">
    <div v-if="fastDomains && fastDomain" class="composer-domain-chip">
      <span>已启用：{{ domainNames[fastDomain] }}<small v-if="fastDomain === 'comic'"> · 漫剧制作</small></span>
      <button type="button" aria-label="退出快捷模式" :disabled="fastDomainBusy" @click="selectDomain(null)"><X :size="13" /></button>
    </div>
    <textarea ref="input" v-model="value" rows="1" placeholder="向 Kantoku 描述问题或制作需求" :disabled="disabled" @input="resize" @keydown="keydown"></textarea>
    <footer>
      <div v-if="fastDomains" class="composer-domain-control">
        <button type="button" class="composer-tool composer-plus" aria-label="选择创作域快捷模式" :aria-expanded="menuOpen" aria-haspopup="menu" :disabled="fastDomainBusy" @click="menuOpen = !menuOpen"><Plus :size="18" /></button>
        <div v-if="menuOpen" class="composer-domain-menu" role="menu" aria-label="创作域快捷模式">
          <button v-for="(label, domain) in domainNames" :key="domain" type="button" role="menuitem" @click="selectDomain(domain)"><strong>{{ label }}</strong><small v-if="domain === 'comic'">导演方案 → 生图指令 → 图片优化</small></button>
        </div>
      </div>
      <button v-else type="button" class="composer-tool" title="附件能力即将开放" disabled><Paperclip :size="17" /></button>
      <span>Enter 发送 · Shift+Enter 换行</span>      <button type="button" class="composer-send" :disabled="!canSend" aria-label="发送" @click="submit"><ArrowUp :size="17" /></button>
    </footer>
  </div>
</template>
