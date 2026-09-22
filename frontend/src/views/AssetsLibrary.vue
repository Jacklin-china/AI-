<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import EmptyState from '../components/EmptyState.vue'
import ArtifactCard from '../components/artifacts/ArtifactCard.vue'
import { getArtifacts, getRuns } from '../services/core'
import type { CoreArtifact, CoreRun } from '../types'

const artifacts = ref<CoreArtifact[]>([])
const runs = ref<CoreRun[]>([])
const typeFilter = ref('all')
const domainFilter = ref('all')
const originFilter = ref('all')
const timeFilter = ref('all')
const limit = ref(48)

async function refresh(): Promise<void> {
  const [artifactData, runData] = await Promise.all([getArtifacts(), getRuns()])
  if (artifactData) artifacts.value = artifactData
  if (runData) runs.value = runData
}

const domainOf = (artifact: CoreArtifact): string => {
  const schema = String(artifact.metadata.schema_name ?? '')
  const prefix = schema.split('.')[0] || artifact.source.split('.')[0]
  if (['commerce', 'comic', 'studio'].includes(prefix)) return prefix
  return runs.value.find((run) => run.id === artifact.run_id)?.domain ?? 'studio'
}

const visible = computed(() => artifacts.value.filter((item) => {
  const origin = String(item.metadata.origin ?? (item.metadata.mock ? 'mock' : 'real'))
  if (typeFilter.value !== 'all' && item.type !== typeFilter.value) return false
  if (domainFilter.value !== 'all' && domainOf(item) !== domainFilter.value) return false
  if (originFilter.value !== 'all' && origin !== originFilter.value) return false
  if (timeFilter.value !== 'all') {
    const age = Date.now() - new Date(item.created_at).getTime()
    if (age > Number(timeFilter.value) * 86_400_000) return false
  }
  return true
}))
const displayed = computed(() => visible.value.slice(0, limit.value))

onMounted(() => void refresh())
</script>

<template>
  <main class="page assets-page">
    <header class="page-header">
      <div>
        <span class="section-kicker">全局历史产物</span>
        <h1>资产</h1>
        <p>跨 Run 的产物库；当前任务产物会同时出现在 Workspace 会话中。</p>
      </div>
      <div class="asset-filters">
        <select v-model="domainFilter" class="ui-select" aria-label="按领域筛选">
          <option value="all">全部领域</option><option value="commerce">Commerce</option><option value="comic">Comic</option><option value="studio">Studio</option>
        </select>
        <select v-model="typeFilter" class="ui-select" aria-label="按类型筛选">
          <option value="all">全部类型</option>
          <option value="image">图片</option>
          <option value="video">视频</option>
          <option value="listing">Listing</option>
          <option value="report">报告</option>
          <option value="json">JSON</option>
          <option value="document">文档</option>
        </select>
        <select v-model="originFilter" class="ui-select" aria-label="按来源状态筛选">
          <option value="all">Real / Mock / Blocked</option><option value="real">Real</option><option value="mock">Mock</option><option value="blocked">Blocked</option>
        </select>
        <select v-model="timeFilter" class="ui-select" aria-label="按时间筛选">
          <option value="all">全部时间</option><option value="7">最近 7 天</option><option value="30">最近 30 天</option>
        </select>
      </div>
    </header>

    <p v-if="visible.length" class="asset-result-count">显示 {{ displayed.length }} / {{ visible.length }} 个产物</p>
    <section v-if="visible.length" class="asset-grid">
      <ArtifactCard
        v-for="item in displayed"
        :key="item.id"
        :artifact="item"
        :domain="domainOf(item)"
      />
    </section>
    <button v-if="displayed.length < visible.length" class="ui-button asset-load-more" @click="limit += 48">加载更多</button>
    <EmptyState v-if="!visible.length" title="暂无产物" description="任务产生的 Artifact 会自动进入这里。" />
  </main>
</template>
