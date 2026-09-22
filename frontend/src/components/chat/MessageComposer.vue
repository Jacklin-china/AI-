<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { ArrowUp, Paperclip } from 'lucide-vue-next'
const props = defineProps<{ disabled?: boolean }>()
const emit = defineEmits<{ send: [content: string] }>()
const value = ref('')
const input = ref<HTMLTextAreaElement | null>(null)
const canSend = computed(() => value.value.trim() !== '' && !props.disabled)
function submit(): void { if (!canSend.value) return; const content = value.value.trim(); value.value = ''; emit('send', content); void nextTick(resize) }
function keydown(event: KeyboardEvent): void { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submit() } }
function resize(): void { const el = input.value; if (!el) return; el.style.height = '0'; el.style.height = `${Math.min(el.scrollHeight, 168)}px` }
function fill(text: string): void { value.value = text; void nextTick(() => { resize(); input.value?.focus() }) }
defineExpose({ fill })
</script>
<template><div class="message-composer"><textarea ref="input" v-model="value" rows="1" placeholder="向 Kantoku 描述问题或制作需求" :disabled="disabled" @input="resize" @keydown="keydown"></textarea><footer><button type="button" class="composer-tool" title="附件能力即将开放" disabled><Paperclip :size="17" /></button><span>Enter 发送 · Shift+Enter 换行</span><button type="button" class="composer-send" :disabled="!canSend" aria-label="发送" @click="submit"><ArrowUp :size="17" /></button></footer></div></template>
