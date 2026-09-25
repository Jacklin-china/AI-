<script setup lang="ts">
import AssistantMessageBlock from './AssistantMessageBlock.vue'
defineProps<{ content: string }>()
</script>
<template><AssistantMessageBlock :content="content" live /></template>
