<script setup lang="ts">defineProps<{ content: string }>()</script>
<template><div class="chat-row assistant"><div class="assistant-mark live">K</div><div class="assistant-copy"><p>{{ content }}<span class="stream-caret"></span></p></div></div></template>
