import assert from 'node:assert/strict'
import { placeCalls } from './toolPlaces.ts'

const call = (id, name, state) => ({ id, name, state })
const ids = list => list.map(c => c.id)

// a sub-agent that still works and a message that waits: settled calls, in no run
const agent = call('a', 'sub_agents__start', 'ok')
const reminder = call('r', 'intercom__send', 'ok')
const running = call('w', 'web__search', 'running')
const load = call('l', 'skills__load', 'ok')
const batch = call('b', 'image_generator__generate', 'ok')
const runFirst = call('s1', 'shell__run', 'ok')
const runOther = call('s2', 'shell__run', 'ok')
const calls = [agent, reminder, running, load, batch, runFirst, runOther]
const block = c => c.id === 'b'
const run = c => (c.id === 's1' ? true : c.id === 's2' ? false : null)

{
  const { lines, rows } = placeCalls(calls, true, block, run)
  assert.deepEqual(ids(rows), ['a', 'r', 's1'], 'a settled call is a row, its Stop or Cancel on it')
  assert.deepEqual(ids(lines), ['w', 'l', 'b'], 'a call at work, plumbing and a block draw as lines')
  for (const c of [agent, reminder]) {
    assert.equal(Number(rows.includes(c)) + Number(lines.includes(c)), 1, `${c.name}: one place, never none`)
  }
  assert.ok(!rows.includes(runOther) && !lines.includes(runOther), 'a run\'s other call is in its run\'s row')
}
{
  const { lines, rows } = placeCalls(calls, false, block, run)
  assert.deepEqual(rows, [], 'no list drawn: no rows')
  assert.deepEqual(ids(lines), ids(calls), 'every call may draw its line')
}
