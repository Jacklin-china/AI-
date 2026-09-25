<script setup lang="ts">defineProps<{ content: string; pending?: 'queued' | 'replying' | 'failed' }>()</script>
<template><div class="chat-row user"><div class="user-bubble">{{ content }}<small v-if="pending" class="message-pending">{{ pending === 'queued' ? '等待回复' : pending === 'replying' ? '正在回复' : '发送未完成' }}</small></div></div></template>
