<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { getComicProject, getComicWork, getComicWorkSettings, publishComicWorkSetting, syncComicTaskSettings, type ComicAssetView } from '../../services/core'
const props = defineProps<{ workId: string; taskId?: string; localAssets: ComicAssetView[] }>()
const emit = defineEmits<{ changed: [] }>()
const settings = ref<ComicAssetView[]>([]), selected = ref<string[]>([]), error = ref(''), notice = ref(''), busy = ref(false)
const editing = ref(false), name = ref(''), kind = ref('character'), existing = ref<ComicAssetView | null>(null), source = ref<ComicAssetView | null>(null)
const values = ref<Record<string, string>>({}), constraints = ref('')
const fields: Record<string, Record<string, string>> = {
  character: { appearance: '外貌', outfit: '服装', features: '固定特征（每行一项）' },
  scene: { location: '地点', time_of_day: '时间', weather: '天气', lighting: '光线', atmosphere: '氛围', environment_features: '环境特征（每行一项）' },
  style: { art_direction: '艺术方向', color_language: '色彩', materials: '材质', camera_language: '摄影语言', lighting: '光影' },
}
const activeFields = computed(() => fields[kind.value] ?? {})
async function load() { const id = props.workId; try { const data = await getComicWorkSettings(id); if (id === props.workId) { settings.value = data; selected.value = data.map(item => item.asset_id) } } catch (failure) { error.value = String(failure) } }
function edit(asset: ComicAssetView | null = null, local = false) {
  editing.value = true; source.value = local ? asset : null; existing.value = local ? null : asset
  name.value = asset?.name ?? ''; kind.value = String(asset?.details.kind ?? 'character'); constraints.value = asset?.fixed_constraints.join('\n') ?? ''
  values.value = Object.fromEntries(Object.entries(asset?.details ?? {}).filter(([key]) => key !== 'kind').map(([key, value]) => [key, Array.isArray(value) ? value.join('\n') : String(value)]))
}
function lines(value = '') { return value.split('\n').map(item => item.trim()).filter(Boolean) }
async function publish() {
  if (!name.value.trim()) { error.value = '请填写设定名称'; return }
  const id = props.workId; busy.value = true; error.value = ''; notice.value = ''
  try {
    const work = await getComicWork(id); if (!work) throw new Error('作品读取失败')
    const details = Object.fromEntries(Object.keys(activeFields.value).map(key => [key, ['features', 'environment_features'].includes(key) ? lines(values.value[key]) : values.value[key] ?? '']))
    const asset = source.value ? { name: source.value.name, aliases: source.value.aliases ?? [], details: source.value.details, fixed_constraints: source.value.fixed_constraints, reference_artifact_ids: source.value.reference_artifact_ids, tags: source.value.tags ?? [] } : { name: name.value.trim(), aliases: existing.value?.aliases ?? [], details: { kind: kind.value, ...details }, fixed_constraints: lines(constraints.value), reference_artifact_ids: existing.value?.reference_artifact_ids ?? [], tags: existing.value?.tags ?? [] }
    await publishComicWorkSetting(id, { request_id: crypto.randomUUID(), expected_version: work.version, asset,
      ...(existing.value ? { asset_id: existing.value.asset_id, expected_asset_version: existing.value.version } : {}),
      ...(source.value ? { source_task_id: props.taskId, source_asset_id: source.value.asset_id, source_version: source.value.version } : {}) })
    if (id !== props.workId) return
    notice.value = '作品设定已保存为新版本。其他画面可选择同步。'; editing.value = false; await load(); emit('changed')
  } catch (failure) { if (id === props.workId) error.value = String(failure) } finally { busy.value = false }
}
async function sync() {
  if (!props.taskId) return
  const id = props.workId, taskId = props.taskId; busy.value = true; error.value = ''
  try {
    const project = await getComicProject(taskId); if (!project) throw new Error('画面读取失败')
    await syncComicTaskSettings(id, taskId, { request_id: crypto.randomUUID(), expected_project_version: project.project.current_version,
      refs: settings.value.filter(item => selected.value.includes(item.asset_id)).map(item => ({ asset_id: item.asset_id, version: item.pinned_version || item.version })) })
    if (id === props.workId && taskId === props.taskId) { notice.value = '所选设定已引用到当前画面，下一次方案修订使用这些版本。已采用图片和故事保持原版本。'; emit('changed') }
  } catch (failure) { error.value = String(failure) } finally { busy.value = false }
}
watch(() => props.workId, () => { editing.value = false; void load() })
onMounted(load)
</script>
<template>
  <section class="work-settings" aria-label="作品公共设定">
    <header><strong>作品公共设定</strong><button class="ui-button sm" :disabled="busy" @click="edit()">添加公共设定</button></header>
    <p class="pane-note">当前画面的调整默认只作用于本画面。更新作品设定后，其他画面由你选择同步。</p>
    <p v-if="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <div v-for="asset in settings" :key="asset.asset_id" class="setting-row"><label><input v-model="selected" type="checkbox" :value="asset.asset_id" />{{ asset.name }} · v{{ asset.version }}</label><button class="ui-button quiet sm" @click="edit(asset)">更新作品设定</button></div>
    <button v-if="taskId && settings.length" class="ui-button sm" :disabled="busy" @click="sync">将所选版本同步到当前画面</button>
    <details v-if="localAssets.length"><summary>将画面资产采用为作品设定</summary><div v-for="asset in localAssets.filter(item => item.state === 'active')" :key="asset.asset_id" class="setting-row"><span>{{ asset.name }} · v{{ asset.version }}</span><button class="ui-button sm" :disabled="busy" @click="edit(asset, true)">预览并采用</button></div></details>
    <form v-if="editing" class="setting-editor" @submit.prevent="publish"><p v-if="source">采用后，此资产版本成为公共设定；原始创作来源仍保留。</p><template v-else><label>名称<input v-model="name" required /></label><label>类型<select v-model="kind" :disabled="!!existing"><option value="character">角色</option><option value="scene">世界与场景</option><option value="style">风格</option></select></label><label v-for="(label, key) in activeFields" :key="key">{{ label }}<textarea v-model="values[key]" rows="2" /></label><label>固定要求（每行一项）<textarea v-model="constraints" rows="2" /></label></template><pre v-if="source">{{ source.name }}
{{ Object.entries(source.details).filter(([key]) => key !== 'kind').map(([, value]) => Array.isArray(value) ? value.join('；') : value).join('\n') }}</pre><div><button class="ui-button primary sm" :disabled="busy" type="submit">确认更新作品设定</button><button class="ui-button quiet sm" type="button" @click="editing = false">取消</button></div></form>
  </section>
</template>
<style scoped>
.work-settings { display:grid; gap:12px; margin-bottom:24px; padding:16px; border:1px solid var(--border-muted); border-radius:var(--radius-sm); }
header,.setting-row { display:flex; align-items:center; justify-content:space-between; gap:12px; }.setting-editor { display:grid; gap:12px; max-width:640px; }.setting-editor label { display:grid; gap:6px; }.setting-editor input,.setting-editor textarea,.setting-editor select { font:inherit; padding:8px; color:var(--text-primary); background:var(--surface); border:1px solid var(--border-strong); border-radius:var(--radius-control); }pre { white-space:pre-wrap; font:inherit; }
</style>
