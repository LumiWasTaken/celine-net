import assert from 'node:assert/strict'
import { firstTime, stepsOf } from './featureSteps.ts'

const f = (id, more = {}) => ({ id, label: id, step: false, state: 'none', warn: '', group: '', group_label: '', ...more })
const computer = { group: 'computer', group_label: 'Her computer' }
const catalog = [
  f('shellbox', { step: true, state: 'unset', ...computer }),
  f('mac', { step: true, ...computer }),
  f('shares', { state: 'unset', warn: 'Careful.' }),
  f('browser', { step: true, state: 'ready' }),
  f('email', { step: true, state: 'unset' }),
  f('guard', { step: true, state: 'unset' }),
  f('missions'),
  f('routines', { step: true }),
]
const fresh = { catalog, saved: false, done: false, version: '', tiers: [], core: [],
  features: { shellbox: false, mac: false, shares: false, browser: false, email: false, guard: false, missions: true, routines: true } }

// defaults: a fresh install; the browser answers, so it is on; nothing blocked starts on
const start = firstTime(fresh)
assert.deepEqual(Object.keys(start.features).filter(id => start.features[id]), ['browser', 'missions', 'routines'])
assert.deepEqual(stepsOf(start), ['her', 'you', 'model', 'features', 'browser', 'routines', 'done'])

// the Mac on: her computer's step comes first, once, for the Mac alone or with the shellbox
const mac = { ...start, features: { ...start.features, mac: true } }
assert.deepEqual(stepsOf(mac), ['her', 'you', 'model', 'features', 'computer', 'browser', 'routines', 'done'])
const both = { ...mac, features: { ...mac.features, shellbox: true } }
assert.deepEqual(stepsOf(both), stepsOf(mac))

// a blocked feature switched on keeps its step (its setup is in it); a plain one has none
const more = { ...start, features: { ...start.features, email: true, guard: true, missions: false, browser: false, routines: false } }
assert.deepEqual(stepsOf(more), ['her', 'you', 'model', 'features', 'email', 'guard', 'done'])

// run again after a save: what is saved on and not blocked, nothing turned on by its probe
const saved = firstTime({ ...fresh, saved: true, features: { ...fresh.features, browser: false, shellbox: true } })
assert.equal(saved.features.browser, false)
assert.equal(saved.features.shellbox, false, 'blocked: off')
assert.deepEqual(stepsOf(null), ['her', 'you', 'model', 'features', 'done'])
console.log('onboarding step checks passed')
