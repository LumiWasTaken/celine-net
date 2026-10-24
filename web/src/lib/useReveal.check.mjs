// node --experimental-strip-types src/lib/useReveal.check.mjs (runs before every build)
import assert from 'node:assert/strict'
import { blocks, closeOpen, common, cutAt, step } from './useReveal.ts'

// the pace: a burst of 600 characters drains over about LAG seconds, never in one frame
let p = { shown: 0, rate: 0 }
const per = []
for (let f = 0; f < 120; f++) { const was = p.shown; p = step(p, 600, true, 1 / 60); per.push(p.shown - was) }
assert.ok(Math.max(...per) < 600 / 10, 'no frame paints a tenth of a burst')
assert.ok(p.shown > 590, 'the burst is on screen within two seconds')

// the whole reveal, the catch-up after the last token too: bursts of 1 to 60 words 20 to
// 400 ms apart (how a real server often streams), then a last burst of up to 200 words.
// While text waits, the amount painted changes from frame to frame by under 8 % of the
// busiest frame, the last frame is no burst, and the end is on screen within 0.5 s of the
// last token. A gap in the stream longer than the text held back is a pause, not a jump.
let seed = 1
const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648
for (const last of [6, 120, 360, 600, 1200]) {
  for (let run = 0; run < 4; run++) {
    const bursts = []
    let at = 0.3
    for (let i = 0; i < 12; i++) { bursts.push([at, Math.floor(1 + rnd() * 60) * 6]); at += 0.02 + rnd() * 0.38 }
    bursts.push([at, last])
    let r = { shown: 0, rate: 0 }, length = 0, next = 0, now = 0
    const frames = []                                  // [characters painted, text still waits]
    while (now < at + 2) {
      now += 1 / 60
      while (next < bursts.length && bursts[next][0] <= now) length += bursts[next++][1]
      const live = next < bursts.length
      const was = r.shown
      r = step(r, length, live, 1 / 60)
      if (r.shown > 0) frames.push([r.shown - was, r.shown < length])
      if (!live && r.shown >= length) break
    }
    const end = frames.pop()[0]
    const peak = Math.max(...frames.map(f => f[0]))
    let change = Math.max(0, end - frames.at(-1)[0])
    for (let i = 1; i < frames.length; i++) {
      if (frames[i - 1][1] && frames[i][1]) change = Math.max(change, Math.abs(frames[i][0] - frames[i - 1][0]))
    }
    assert.ok(change / peak < 0.08, `last burst ${last}: a frame changes by ${(change / peak * 100).toFixed(1)} %`)
    assert.ok(now - at <= 0.5, `last burst ${last}: the end ${(now - at).toFixed(2)} s after the last token`)
  }
}

// whole words, and never inside a character
assert.equal(cutAt('hello world again', 9), 6)
assert.equal(cutAt('ab', 5), 2)
const emoji = 'x'.repeat(30) + '👩‍💻' + 'y'.repeat(30)
const c = cutAt(emoji, 31)
assert.ok(c === 30 || c >= 30 + '👩‍💻'.length, `cut ${c} is not inside the emoji`)
assert.equal(common('abcdef', 'abcxyz'), 3)

// blocks split at blank lines, never inside a fence or before an indented line
assert.deepEqual(blocks('a\n\nb'), ['a', 'b'])
assert.deepEqual(blocks('```\nx\n\ny\n```\n\nz').length, 2)
assert.deepEqual(blocks('1. one\n\n   more\n\n2. two').length, 2)

// open markers closed in the last block
assert.equal(closeOpen('some **bold'), 'some **bold**')
assert.equal(closeOpen('an *aside'), 'an *aside*')
assert.equal(closeOpen('* a list item'), '* a list item')
assert.equal(closeOpen('snake_case_name'), 'snake_case_name')
assert.equal(closeOpen('call `fn'), 'call `fn`')
assert.equal(closeOpen('```py\nx = 1'), '```py\nx = 1\n```')
assert.equal(closeOpen('see [the docs](https://exa'), 'see the docs')
assert.equal(closeOpen('look ![a cat](http'), 'look')
assert.equal(closeOpen('as said [[kb:12'), 'as said')
assert.equal(closeOpen('~~gone'), '~~gone~~')
assert.equal(closeOpen('done **here** and *there*'), 'done **here** and *there*')

console.log('useReveal: ok')
