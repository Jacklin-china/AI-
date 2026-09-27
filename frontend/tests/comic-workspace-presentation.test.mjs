import test from 'node:test'
import assert from 'node:assert/strict'
import { comicPhases, resolveComicPreviewUrl } from '../src/domains/comic/workspacePresentation.ts'

const node = (node_id, status) => ({ node_id, status })
const run = (status, current_node, nodes) => ({ status, current_node, nodes })

test('no run has no fake storyboard or workflow progress', () => {
  assert.deepEqual(comicPhases(null), [])
})

test('generation shows only real completed and active nodes', () => {
  const phases = comicPhases(run('running', 'generate', [
    node('prepare', 'completed'), node('cost_approval', 'completed'), node('generate', 'running'),
  ]))
  assert.deepEqual(phases.map(({ label, status }) => [label, status]), [
    ['准备', 'completed'], ['费用', 'completed'], ['生成', 'running'],
    ['检查', 'pending'], ['审核', 'pending'], ['交付', 'pending'],
  ])
  assert.equal(phases.some(({ label }) => label === '分镜' || label === '角色设计'), false)
})

test('approval waits and completed delivery are sourced from the run', () => {
  const waiting = comicPhases(run('waiting', 'human_review', [
    node('prepare', 'completed'), node('cost_approval', 'completed'),
    node('generate', 'completed'), node('qc', 'completed'), node('human_review', 'waiting'),
  ]))
  assert.equal(waiting.find((phase) => phase.id === 'review')?.status, 'waiting')
  assert.equal(waiting.find((phase) => phase.id === 'delivery')?.status, 'pending')

  const completed = comicPhases(run('completed', '__end__', [
    node('archive', 'completed'),
  ]))
  assert.equal(completed.find((phase) => phase.id === 'delivery')?.status, 'completed')
})

test('historical image archive falls back to the original real task image', async () => {
  const calls = []
  const url = await resolveComicPreviewUrl(
    { id: 'archive-image', type: 'image' },
    'original-request',
    async (id) => { calls.push(`artifact:${id}`); return null },
    async (id) => { calls.push(`task:${id}`); return 'blob:real-image' },
  )
  assert.equal(url, 'blob:real-image')
  assert.deepEqual(calls, ['artifact:archive-image', 'task:original-request'])
})

test('video preview does not substitute a task image for a missing video', async () => {
  const url = await resolveComicPreviewUrl(
    { id: 'archive-video', type: 'video' },
    'original-request',
    async () => null,
    async () => { throw new Error('must not load an unrelated image') },
  )
  assert.equal(url, null)
})
