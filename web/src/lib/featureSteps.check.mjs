import assert from 'node:assert/strict'
import { firstTime, landing, stepsOf, toSave } from './featureSteps.ts'

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

// run again after a save: what is saved, a blocked one too, nothing turned on by its probe
const stored = { ...fresh.features, browser: false, shellbox: true }
const saved = firstTime({ ...fresh, saved: true, features: stored })
assert.equal(saved.features.browser, false)
assert.equal(saved.features.shellbox, true, 'saved on, blocked for now: still on')
assert.deepEqual(stepsOf(saved).slice(4, 5), ['computer'])
// a PUT keeps a blocked switch that was saved on, and saves a blocked one off that was not
const put = toSave({ ...saved, features: { ...saved.features, email: true } }, stored)
assert.equal(put.shellbox, true)
assert.equal(put.email, false)
assert.equal(toSave({ ...saved, features: { ...saved.features, shellbox: false } }, stored).shellbox, false, 'switched off: off')

// a URL's step: itself while on; a group's feature, the group; one off, the next one on, else Features
assert.equal(landing(mac, 'routines'), 'routines')
assert.equal(landing(mac, 'shellbox'), 'computer')
assert.equal(landing(start, 'computer'), 'browser')
assert.equal(landing(start, 'email'), 'routines')
assert.equal(landing(more, 'routines'), 'features')
assert.equal(landing(start, 'nonsense'), 'features')
assert.deepEqual(stepsOf(null), ['her', 'you', 'model', 'features', 'done'])
console.log('onboarding step checks passed')
