<script setup lang="ts">defineProps<{ label: string; tone?: 'neutral' | 'active' | 'warning' }>()</script>
<template><div class="system-inline" :data-tone="tone ?? 'neutral'"><span></span>{{ label }}</div></template>
