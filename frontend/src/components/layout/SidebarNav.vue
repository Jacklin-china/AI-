<script setup lang="ts">
import {
  Boxes,
  ChevronsLeft,
  ChevronsRight,
  Frame,
  History,
  House,
  Images,
  Settings,
  TableProperties,
} from 'lucide-vue-next'
import { type FunctionalComponent } from 'vue'

import KantokuMark from '../brand/KantokuMark.vue'

defineProps<{ routeName: string; activeDomainId: string; taskCount: number; collapsed: boolean; rail?: boolean }>()
const emit = defineEmits<{ navigate: [path: string]; toggle: [] }>()

/* 「工作区」就是漫剧制作（design-spec §1 收敛后唯一创作的领域）。
   此前它依赖一个通用「与 Kantoku 协作」工作区，并另设「创作域」分组重复指向同一个漫剧入口；
   现在两者合并为一个条目：点击直接落到漫剧制作的工作台首页，不再经过领域选择层。 */
const navItems: { name: string; label: string; path: string; icon: FunctionalComponent; match: string[] }[] = [
  { name: 'home', label: '首页', path: '/', icon: House, match: ['home'] },
  { name: 'workspace', label: '工作区', path: '/workspace/comic', icon: Frame, match: ['workspace', 'workspace_run'] },
  { name: 'tasks', label: '任务中心', path: '/tasks', icon: TableProperties, match: ['tasks', 'task_run', 'task_batch'] },
  { name: 'assets', label: '资产', path: '/assets', icon: Images, match: ['assets'] },
  { name: 'history', label: '历史记录', path: '/tasks/history', icon: History, match: [] },
]
</script>

<template>
  <aside class="app-sidebar" :class="{ collapsed, 'global-icon-rail': rail }" aria-label="全局导航">
    <div class="sidebar-brand-row"><button class="product-brand" title="Kantoku" @click="emit('navigate', '/')"><KantokuMark /><span class="brand-copy"><strong>Kantoku</strong><small>PRODUCTION WORKSPACE</small></span></button><button class="sidebar-toggle" :title="collapsed ? '展开侧栏' : '折叠侧栏'" @click="emit('toggle')"><ChevronsLeft v-if="!collapsed" :size="13" /><ChevronsRight v-else :size="13" /></button></div>
    <nav class="nav-group" aria-label="产品导航"><span class="nav-label">创作</span><button v-for="item in navItems" :key="item.name" :class="{ active: item.match.includes(routeName) }" :title="item.label" @click="emit('navigate', item.path)"><i><component :is="item.icon" :size="18" :stroke-width="1.7" /></i><span>{{ item.label }}</span><em v-if="item.name === 'runs' && taskCount">{{ taskCount }}</em></button></nav>
    <div class="sidebar-bottom"><button title="模型与供应商" @click="emit('navigate', '/settings')"><i><Boxes :size="18" :stroke-width="1.7" /></i><span>模型与供应商</span></button><button title="设置" @click="emit('navigate', '/settings')"><i><Settings :size="18" :stroke-width="1.7" /></i><span>设置</span></button><div class="core-health" title="Kantoku Core · 本机已连接"><span></span><div><b>Kantoku Core</b><small>本机 · 已连接</small></div></div></div>
  </aside>
</template>
