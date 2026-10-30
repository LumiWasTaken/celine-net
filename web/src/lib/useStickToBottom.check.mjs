// node --experimental-strip-types src/lib/useStickToBottom.check.mjs (runs before every build)
import assert from 'node:assert/strict'
import { FAR, REPIN, gapOf, glide, scrolledUp } from './useStickToBottom.ts'

// the two thresholds: pin again close to the bottom, "New messages" only far above it
assert.ok(REPIN <= 32 && FAR >= 200 && FAR > REPIN)
assert.equal(gapOf({ scrollHeight: 1000, clientHeight: 400, scrollTop: 600 }), 0)
assert.equal(gapOf({ scrollHeight: 1000, clientHeight: 400, scrollTop: 590 }), 10)

// a scroll up by you lets go; content that shrinks under a view at the bottom clamps the
// top lower by itself, which is no scroll of yours (the view stays pinned)
assert.ok(scrolledUp(600, 500, 100))
assert.ok(!scrolledUp(600, 588, 0), 'a clamp at the bottom')
assert.ok(!scrolledUp(600, 599.5, 40), 'under a pixel is no scroll')
assert.ok(!scrolledUp(500, 600, 0))

// your jump glides to the bottom in a few frames and lands on it exactly
let top = 0, frames = 0
while (top < 3000 && frames < 100) { top = glide(top, 3000); frames++ }
assert.equal(top, 3000)
assert.ok(frames > 5 && frames < 40, `${frames} frames`)
assert.equal(glide(2999, 3000), 3000)

console.log('useStickToBottom: ok')
