import test from 'node:test'
import assert from 'node:assert/strict'
import { renderInlineMarkdown } from '../src/components/chat/inlineMarkdown.ts'

test('public source citations render as safe links instead of raw Markdown URLs', () => {
  const html = renderInlineMarkdown('今天有雨 [天气网](https://www.tianqi.com/beijing/today/)')
  assert.match(html, /<a href="https:\/\/www\.tianqi\.com\/beijing\/today\/"/)
  assert.match(html, /rel="noopener noreferrer">天气网<\/a>/)
  assert.doesNotMatch(html, /\]\(/)
})

test('unsafe links and labels are escaped', () => {
  const html = renderInlineMarkdown('[<img src=x>](javascript:alert(1))')
  assert.doesNotMatch(html, /<img/)
  assert.doesNotMatch(html, /href="javascript:/)
})
