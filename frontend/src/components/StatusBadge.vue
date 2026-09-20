<script setup lang="ts">
defineProps<{ status: string; label?: string }>()

const labels: Record<string, string> = {
  running: '运行中', waiting: '等待', completed: '已完成', succeeded: '已完成',
  failed: '失败', cancelled: '已取消', unknown: '待核对', draft: '草稿',
  pending: '待启动', available: '可用', preview: '预览', coming_soon: '待上线',
}
</script>

<template>
  <span class="status-badge" :data-status="status"><i></i>{{ label ?? labels[status] ?? status }}</span>
</template>
