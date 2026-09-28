import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/domains/comic/directorPresentation.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const { visibleDirectorNodes, editableDirectorNode, selectDirectorExecution } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

test('Fast never exposes director node details; Professional uses the same real summary', () => {
  const stages = [{ stage: 'creative_understanding' }, { stage: 'director_critic' }]
  assert.deepEqual(visibleDirectorNodes('fast', stages), [])
  assert.equal(visibleDirectorNodes('professional', stages), stages)
  assert.equal(editableDirectorNode('director_critic'), false)
  assert.equal(editableDirectorNode('visual_direction'), true)
})

test('a new request cannot reuse the previous completed task as current progress', () => {
  const old = { run_id: 'old', status: 'completed' }
  const current = { run_id: 'current', status: 'running' }
  assert.equal(selectDirectorExecution([old], 'old', true, ['old']), undefined)
  assert.equal(selectDirectorExecution([current, old], 'old', true, ['old']), current)
  assert.equal(selectDirectorExecution([current, old], 'old', false, []), old)
})
