<script setup lang="ts">
import AssistantMessageBlock from './AssistantMessageBlock.vue'
defineProps<{ content: string; showMark?: boolean }>()
</script>
<template><AssistantMessageBlock :content="content" :show-mark="showMark" live /></template>
