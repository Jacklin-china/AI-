<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { previewComicLegacy, importComicLegacy, getArtifactContentUrl, type ComicLegacyPreview, type ComicWork } from '../../services/core'
const props = defineProps<{ conversationId: string }>()
const emit = defineEmits<{ imported: [work: ComicWork] }>()
const preview = ref<ComicLegacyPreview | null>(null), selected = ref<string[]>([]), title = ref(''), includeStory = ref(false)
const open = ref(false), busy = ref(false), error = ref(''), pending = ref<Record<string, unknown> | null>(null), urls = ref<Record<string, string>>({})
const dialog = ref<HTMLElement | null>(null)
async function load() {
  open.value = true; busy.value = true; error.value = ''
  void nextTick(() => dialog.value?.focus())
  const owner = props.conversationId
  try {
    const result = await previewComicLegacy(owner)
    if (owner !== props.conversationId) return
    preview.value = result; selected.value = []; includeStory.value = false; pending.value = null
    title.value = ''
    for (const image of result?.groups.flatMap(group => group.images.slice(0, 3)) ?? []) {
      const url = await getArtifactContentUrl(image.artifact_id, true)
      if (!url) continue
      if (owner !== props.conversationId) URL.revokeObjectURL(url)
      else urls.value[image.artifact_id] = url
    }
  } catch (failure) { error.value = String(failure) } finally { busy.value = false }
}
async function confirm() {
  if (!preview.value || !title.value.trim() || !selected.value.length) { error.value = '填写新作品名称并明确选择来源。'; return }
  const owner = props.conversationId
  pending.value ??= { request_id: crypto.randomUUID(), title: title.value.trim(), conversation_id: owner,
    preview_sha256: preview.value.preview_sha256, source_ids: [...selected.value], include_story: includeStory.value }
  busy.value = true; error.value = ''
  try { const result = await importComicLegacy(pending.value); if (owner === props.conversationId) { open.value = false; emit('imported', result) } }
  catch (failure) { error.value = String(failure) } finally { busy.value = false }
}
function clear() { Object.values(urls.value).forEach(url => URL.revokeObjectURL(url)); urls.value = {}; preview.value = null; open.value = false; pending.value = null }
watch(() => props.conversationId, clear)
onBeforeUnmount(clear)
</script>
<template>
  <section class="legacy-import" aria-label="历史内容归入作品">
    <p>当前为历史对话。原方案、图片和聊天保留在原来源中。</p><button class="ui-button sm" :disabled="busy" @click="load">预览归入新作品</button>
    <div v-if="open" class="legacy-overlay" @click.self="!busy && clear()"><div ref="dialog" class="legacy-preview" role="dialog" aria-modal="true" aria-label="历史内容归入预览" tabindex="-1" @keydown.esc="!busy && clear()"><header><strong>历史内容归入预览</strong><button class="ui-button quiet sm" :disabled="busy" @click="clear">关闭预览</button></header><p v-if="busy" role="status">正在读取或归入历史内容…</p><p v-if="error" role="alert">{{ error }}</p><template v-if="preview">
      <label>新作品名称<input v-model="title" :disabled="busy || !!pending" /></label>
      <p>仅归入明确勾选的来源，每份来源建立独立画面与对话。</p>
      <section v-for="group in preview.groups" :key="group.work_id"><label><input v-model="selected" type="checkbox" :value="group.work_id" :disabled="busy || !!pending" />{{ group.title }}</label><p>{{ group.original_request }}</p><small>原项目 {{ group.project_id }} · Brief {{ group.brief_version ?? '未绑定' }} · {{ group.has_director ? '有导演方案' : '仅图片' }} · {{ group.messages.length }} 条可明确归属的消息</small><div class="legacy-images"><img v-for="image in group.images.filter(image => urls[image.artifact_id])" :key="image.artifact_id" :src="urls[image.artifact_id]" :alt="image.filename" /></div><small>{{ group.images.length }} 个图片版本；原版本与采用来源保留映射</small></section>
      <label v-if="preview.story"><input v-model="includeStory" type="checkbox" :disabled="busy || !!pending" />将《{{ preview.story.title }}》导入为待核验故事草稿</label><p v-if="preview.unassigned_message_ids.length">{{ preview.unassigned_message_ids.length }} 条消息归属不明确，将留在原对话供查看。</p>
      <p>确认后先备份数据库，再追加新身份与来源映射。旧确认不沿用，不会调用模型。</p>
      <button class="ui-button primary sm" :disabled="busy" @click="confirm">{{ pending ? '继续原归入请求' : '确认归入新作品' }}</button>
    </template></div></div>
  </section>
</template>
<style scoped>
.legacy-import { margin:12px 0; color:var(--text-secondary); font-size:var(--font-meta); }.legacy-preview { display:grid; gap:12px; padding:16px; border:1px solid var(--border-muted); }.legacy-preview section { padding:12px 0; border-bottom:1px solid var(--border-muted); }.legacy-preview input:not([type=checkbox]) { margin-left:12px; padding:8px; font:inherit; }.legacy-images { display:flex; gap:8px; flex-wrap:wrap; margin:12px 0; }.legacy-images img { width:120px; height:90px; object-fit:contain; }
.legacy-overlay { position:fixed; inset:0; z-index:100; display:grid; place-items:center; padding:24px; background:rgba(0,0,0,.35); }
.legacy-preview { width:min(800px,100%); max-height:calc(100dvh - 48px); overflow:auto; box-sizing:border-box; color:var(--text-primary); background:var(--surface); border-radius:var(--radius-panel); box-shadow:var(--shadow-card); overflow-wrap:anywhere; }
.legacy-preview header { display:flex; justify-content:space-between; align-items:center; gap:12px; }
</style>
