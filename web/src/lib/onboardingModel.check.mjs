import assert from 'node:assert/strict'
import { onboardingModel } from './onboardingModel.ts'

for (const id of ['chat-model-one', 'chat-model-two']) {
  const saved = { label: 'My model', caps: ['chat', 'tools', 'vision'], context: 32768,
    slots: 2, params: { temperature: 0.5 }, thinking_levels: ['low', 'high'] }
  const result = onboardingModel(id, { id, name: id, caps: ['chat', 'tools'], context: 8192 }, saved)
  assert.deepEqual(result, saved)
  assert.ok(result.caps.includes('vision'))
  assert.notEqual(result.caps, saved.caps)
}
assert.deepEqual(onboardingModel('new-model'), { label: 'new-model', caps: ['chat'] })
assert.deepEqual(onboardingModel('new-model', { id: 'new-model', caps: ['chat', 'vision'] }).caps, ['chat', 'vision'])
assert.deepEqual(onboardingModel('old-model', undefined, { caps: ['chat', 'vision'] }).caps, ['chat', 'vision'])
for (const caps of [['chat'], []]) {
  assert.deepEqual(onboardingModel('existing', { id: 'existing', caps: ['chat', 'vision', 'tools'] }, { caps }).caps, caps,
    'discovery must not re-enable explicitly disabled capabilities')
}
console.log('onboarding model preservation checks passed')
