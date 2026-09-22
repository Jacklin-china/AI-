<script setup lang="ts">defineProps<{ title: string; description?: string }>()</script>
<template><section class="artifact-message"><strong>{{ title }}</strong><p v-if="description">{{ description }}</p></section></template>
