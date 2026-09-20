<script setup lang="ts">
import {
  Boxes,
  Frame,
  History,
  House,
  Images,
  Search,
  Settings,
  Sparkles,
  TableProperties,
} from 'lucide-vue-next'
import { computed, nextTick, ref, watch, type FunctionalComponent } from 'vue'

import { domains } from '../../domains'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: []; navigate: [path: string]; domain: [id: string] }>()
const query = ref('')
const activeIndex = ref(0)
const input = ref<HTMLInputElement | null>(null)
interface Command { label: string; hint: string; path: string; icon: FunctionalComponent }
const commands: Command[] = [
  { label: '打开首页', hint: '首页', path: '/', icon: House },
  { label: '打开工作台', hint: '工作台', path: '/workspace', icon: Frame },
  { label: '查看任务与运行', hint: '运行', path: '/runs', icon: TableProperties },
  { label: '浏览资产', hint: '资产', path: '/assets', icon: Images },
  { label: '查看历史记录', hint: '历史', path: '/history', icon: History },
  { label: '查看技能', hint: '技能', path: '/skills', icon: Sparkles },
  { label: '模型与供应商', hint: '设置', path: '/settings', icon: Settings },
]
const filtered = computed(() => commands.filter((command) => `${command.label} ${command.hint}`.toLowerCase().includes(query.value.toLowerCase())))
watch(() => props.open, (open) => { if (open) { query.value = ''; activeIndex.value = 0; void nextTick(() => input.value?.focus()) } })
watch(query, () => { activeIndex.value = 0 })
function go(path: string): void { emit('navigate', path); emit('close') }
function move(step: number): void { if (!filtered.value.length) return; activeIndex.value = (activeIndex.value + step + filtered.value.length) % filtered.value.length }
function confirm(): void { const target = filtered.value[activeIndex.value]; if (target) go(target.path) }
</script>

<template>
  <div v-if="open" class="command-backdrop" @click.self="emit('close')" @keydown.esc="emit('close')"><section class="command-palette" role="dialog" aria-modal="true" aria-label="Command Palette"><label><Search :size="15" :stroke-width="2" /><input ref="input" v-model="query" placeholder="搜索页面、Domain 或操作…" @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)" @keydown.enter="confirm" /></label><div class="command-section"><small>页面导航</small><button v-for="(command, index) in filtered" :key="command.path" :class="{ active: index === activeIndex }" @click="go(command.path)" @mouseenter="activeIndex = index"><i><component :is="command.icon" :size="14" :stroke-width="1.8" /></i><span>{{ command.label }}</span><kbd>{{ command.hint }}</kbd></button><p v-if="!filtered.length" class="command-empty">没有匹配的命令</p></div><div class="command-section"><small>创作域</small><button v-for="domain in domains" :key="domain.id" @click="emit('domain', domain.id); emit('close')"><i><Boxes :size="14" :stroke-width="1.8" /></i><span>{{ domain.name }} · {{ domain.label }}</span><kbd>{{ domain.status }}</kbd></button></div><footer><span>↑↓ 选择</span><span>Enter 打开</span><span>Esc 关闭</span></footer></section></div>
</template>
