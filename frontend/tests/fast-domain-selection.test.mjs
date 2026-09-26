import test from 'node:test'
import assert from 'node:assert/strict'
import { visibleFastDomain } from '../src/components/chat/fastDomainSelection.ts'

test('a selected fast domain is visible only before its one task is sent', () => {
  assert.equal(visibleFastDomain('comic', null, false), 'comic')
  assert.equal(visibleFastDomain('comic', null, true), null)
  assert.equal(visibleFastDomain('comic', 'generation-123', false), null)
  assert.equal(visibleFastDomain(null, null, false), null)
})

test('other domains and modes cannot appear as a home fast skill', () => {
  assert.equal(visibleFastDomain('commerce', null, false), 'commerce')
  assert.equal(visibleFastDomain('studio', null, false), 'studio')
  assert.equal(visibleFastDomain('ads', null, false), null)
})
