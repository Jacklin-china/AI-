<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ content: string; live?: boolean }>()

function escapeHtml(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

function inline(value: string): string {
  return escapeHtml(value)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
}

function markdown(value: string): string {
  const lines = value.replace(/\r\n/g, '\n').split('\n')
  const result: string[] = []
  let inCode = false
  let paragraph: string[] = []
  let listType = ''
  let listDepth = 0
  const flushParagraph = (): void => {
    if (paragraph.length) result.push(`<p>${paragraph.map(inline).join('<br>')}</p>`)
    paragraph = []
  }
  const closeList = (): void => {
    while (listDepth > 0) { result.push(`</${listType}>`); listDepth -= 1 }
    listType = ''
  }
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]
    if (/^\s*```/.test(line)) {
      flushParagraph(); closeList()
      result.push(inCode ? '</code></pre>' : '<pre class="message-code"><code>')
      inCode = !inCode
      continue
    }
    if (inCode) { result.push(`${escapeHtml(line)}\n`); continue }
    if (!line.trim()) { flushParagraph(); closeList(); continue }
    const heading = line.match(/^(#{1,3})\s+(.+)$/)
    if (heading) {
      flushParagraph(); closeList()
      const level = heading[1].length + 1
      result.push(`<h${level}>${inline(heading[2])}</h${level}>`)
      continue
    }
    if (/^\|.+\|\s*$/.test(line) && /^\|?[\s:|-]+\|?[\s:|-]*$/.test(lines[index + 1] ?? '')) {
      flushParagraph(); closeList()
      const headers = line.split('|').slice(1, -1)
      result.push(`<div class="message-table-wrap"><table><thead><tr>${headers.map((cell) => `<th>${inline(cell.trim())}</th>`).join('')}</tr></thead><tbody>`)
      index += 2
      while (index < lines.length && /^\|.+\|\s*$/.test(lines[index])) {
        const cells = lines[index].split('|').slice(1, -1)
        result.push(`<tr>${cells.map((cell) => `<td>${inline(cell.trim())}</td>`).join('')}</tr>`)
        index += 1
      }
      result.push('</tbody></table></div>')
      index -= 1
      continue
    }
    const item = line.match(/^(\s*)([-*]|\d+\.)\s+(.+)$/)
    if (item) {
      flushParagraph()
      const type = /\d/.test(item[2]) ? 'ol' : 'ul'
      const depth = Math.min(2, Math.floor(item[1].length / 2)) + 1
      if (listType !== type) closeList()
      while (listDepth < depth) { result.push(`<${type}>`); listDepth += 1 }
      while (listDepth > depth) { result.push(`</${type}>`); listDepth -= 1 }
      listType = type
      result.push(`<li>${inline(item[3])}</li>`)
      continue
    }
    if (line.startsWith('> ')) {
      flushParagraph(); closeList()
      result.push(`<blockquote>${inline(line.slice(2))}</blockquote>`)
      continue
    }
    closeList(); paragraph.push(line)
  }
  flushParagraph(); closeList()
  if (inCode) result.push('</code></pre>')
  return result.join('')
}

const rendered = computed(() => markdown(props.content))
</script>

<template>
  <div class="chat-row assistant"><div class="assistant-mark" :class="{ live }">K</div><div class="assistant-copy"><div class="assistant-rich" v-html="rendered"></div><span v-if="live" class="stream-caret" aria-label="正在回复"></span></div></div>
</template>
