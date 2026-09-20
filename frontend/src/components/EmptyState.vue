<script setup lang="ts">
withDefaults(defineProps<{ title: string; description: string; icon?: string }>(), { icon: '◇' })
</script>

<template>
  <div class="empty-state"><span>{{ icon }}</span><strong>{{ title }}</strong><p>{{ description }}</p><slot /></div>
</template>
