<script setup lang="ts">
import {
  BookOpen,
  Boxes,
  ChevronsLeft,
  ChevronsRight,
  Frame,
  History,
  House,
  Images,
  Layers3,
  Megaphone,
  Palette,
  Settings,
  ShoppingBag,
  Sparkles,
  TableProperties,
  UserCheck,
} from 'lucide-vue-next'
import type { FunctionalComponent } from 'vue'

import { domains, type DomainDefinition } from '../../domains'
import StatusBadge from '../StatusBadge.vue'

defineProps<{ routeName: string; activeDomainId: string; taskCount: number; collapsed: boolean }>()
const emit = defineEmits<{ navigate: [path: string]; domain: [domain: DomainDefinition]; toggle: [] }>()

const navItems: { name: string; label: string; path: string; icon: FunctionalComponent; match: string[] }[] = [
  { name: 'home', label: '首页', path: '/', icon: House, match: ['dashboard'] },
  { name: 'workspace', label: '工作台', path: '/workspace', icon: Frame, match: ['workspace'] },
  { name: 'runs', label: '任务与运行', path: '/runs', icon: TableProperties, match: ['runs', 'run_detail'] },
  { name: 'approvals', label: '审批', path: '/approvals', icon: UserCheck, match: ['approvals'] },
  { name: 'batches', label: '批量任务', path: '/batches', icon: Layers3, match: ['batches'] },
  { name: 'assets', label: '资产', path: '/assets', icon: Images, match: ['assets'] },
  { name: 'history', label: '历史记录', path: '/history', icon: History, match: ['history'] },
  { name: 'skills', label: '技能', path: '/skills', icon: Sparkles, match: ['skills'] },
]
const domainIcons: Record<string, FunctionalComponent> = { comic: BookOpen, commerce: ShoppingBag, ads: Megaphone, studio: Palette }
</script>

<template>
  <aside class="app-sidebar" :class="{ collapsed }">
    <div class="sidebar-brand-row"><button class="product-brand" title="Kantoku" @click="emit('navigate', '/')"><span class="brand-symbol">K</span><span class="brand-copy"><strong>Kantoku</strong><small>AI 生产监督</small></span></button><button class="sidebar-toggle" :title="collapsed ? '展开侧栏' : '折叠侧栏'" @click="emit('toggle')"><ChevronsLeft v-if="!collapsed" :size="13" /><ChevronsRight v-else :size="13" /></button></div>
    <nav class="nav-group" aria-label="产品导航"><span class="nav-label">创作</span><button v-for="item in navItems" :key="item.name" :class="{ active: item.match.includes(routeName) }" :title="item.label" @click="emit('navigate', item.path)"><i><component :is="item.icon" :size="15" :stroke-width="1.8" /></i><span>{{ item.label }}</span><em v-if="item.name === 'runs' && taskCount">{{ taskCount }}</em></button></nav>
    <nav class="nav-group domain-nav" aria-label="Domain 导航"><span class="nav-label">创作域</span><button v-for="domain in domains" :key="domain.id" :class="{ active: routeName === 'domain' && activeDomainId === domain.id }" :title="`${domain.name} ${domain.label}`" @click="emit('domain', domain)"><i><component :is="domainIcons[domain.id]" :size="15" :stroke-width="1.8" /></i><span>{{ domain.name }} <small>{{ domain.label }}</small></span><StatusBadge v-if="domain.status !== 'available' && !collapsed" :status="domain.status" /></button></nav>
    <div class="sidebar-bottom"><button title="模型与供应商" @click="emit('navigate', '/settings')"><i><Boxes :size="15" :stroke-width="1.8" /></i><span>模型与供应商</span></button><button title="设置" @click="emit('navigate', '/settings')"><i><Settings :size="15" :stroke-width="1.8" /></i><span>设置</span></button><div class="core-health" title="Kantoku Core · 本机已连接"><span></span><div><b>Kantoku Core</b><small>本机 · 已连接</small></div></div></div>
  </aside>
</template>
