import assert from 'node:assert/strict'
import { speechChunks, playSpeechQueue } from './speechText.ts'
import { playSpeechAudio } from './speechAudio.ts'

const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

assert.deepEqual(speechChunks('Hello **world**.\n```js\nsecret()\n```\n[Read this](https://example.test) <|speaker_id_0|> https://hidden.test'), ['Hello world.', 'Read this'])
assert.deepEqual(speechChunks('```unfinished code'), [])
for (const code of [
  '````markdown\n```js\nsecret()\n```\n````',
  '~~~~text\n~~~\nsecret()\n~~~\n~~~~',
  '    secret()\n    more_secret()',
  '\tsecret()',
  '> ````\n> ```\n> secret()\n> ```\n> ````',
  '- Example:\n\n      secret()',
]) {
  const spoken = speechChunks(`Before.\n\n${code}\n\nAfter.`).join(' ')
  assert.ok(!spoken.includes('secret'), `code must not be synthesized: ${code}`)
  assert.ok(spoken.includes('Before.') && spoken.includes('After.'))
}
assert.deepEqual(speechChunks('[Readable][ref]\n\n[ref]: https://hidden.test\n\n![secret](image.png)'), ['Readable'])
const long = 'x'.repeat(1900)
const split = speechChunks(long)
assert.ok(split.every(s => s.length <= 900))
assert.equal(split.join(''), long)
assert.equal(speechChunks('Short. Another sentence.').join(' '), 'Short. Another sentence.')

// Generation runs ahead while the first clip is held; never two requests in flight.
{
  const first = deferred(), second = deferred(), release = deferred()
  const generated = [], played = []
  let flight = 0, max = 0
  const c = new AbortController()
  const work = playSpeechQueue(['one', 'two', 'three'], {
    signal: c.signal,
    generate: async text => {
      flight++; max = Math.max(max, flight); generated.push(text); flight--
      if (text === 'two') second.resolve()
      return text
    },
    play: async value => { played.push(value); if (value === 'one') { first.resolve(); await release.promise } },
  })
  await first.promise; await second.promise
  assert.deepEqual(played, ['one'])
  assert.ok(generated.includes('two'))
  release.resolve(); await work
  assert.deepEqual(played, ['one', 'two', 'three']); assert.equal(max, 1)
}

// A paused clip permits only one queued clip, and Stop releases the capacity wait.
{
  const second = deferred(), release = deferred(), c = new AbortController()
  const generated = [], played = []
  const work = playSpeechQueue(Array.from({ length: 100 }, (_, i) => String(i)), {
    signal: c.signal,
    generate: async text => { generated.push(text); if (text === '1') second.resolve(); return text },
    play: async (value, signal) => {
      played.push(value)
      signal.addEventListener('abort', () => release.resolve(), { once: true })
      await release.promise
    },
  })
  await second.promise
  // An event-loop checkpoint drains generation microtasks without a timed sleep.
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(generated, ['0', '1'])
  c.abort(); await assert.rejects(work, { name: 'AbortError' })
  assert.deepEqual(played, ['0'])
  assert.deepEqual(generated, ['0', '1'])
}

// A generation failure after lookahead is still reported in playback order.
{
  const played = []
  await assert.rejects(playSpeechQueue(['one', 'two', 'three'], {
    signal: new AbortController().signal,
    generate: async text => { if (text === 'three') throw new Error('synthesis failed'); return text },
    play: async value => { played.push(value) },
  }), /synthesis failed/)
  assert.deepEqual(played, ['one', 'two'])
}

// Abort during lookahead, even when an upstream ignores cancellation.
{
  const started = deferred(), late = deferred(), c = new AbortController()
  const played = [], generated = []
  const work = playSpeechQueue(['one', 'two'], {
    signal: c.signal,
    generate: async text => { generated.push(text); started.resolve(); await late.promise; return text },
    play: async value => { played.push(value) },
  })
  await started.promise; c.abort()
  await assert.rejects(work, { name: 'AbortError' })
  late.resolve(); await Promise.resolve(); await Promise.resolve()
  assert.deepEqual(played, []); assert.deepEqual(generated, ['one'])
}

// Failed playback cancels lookahead and cannot play buffered clips.
{
  const c = new AbortController(), played = [], generated = [], second = deferred()
  const work = playSpeechQueue(['one', 'two', 'three'], {
    signal: c.signal,
    generate: async text => { generated.push(text); if (text === 'two') second.resolve(); return text },
    play: async value => { played.push(value); await second.promise; throw new Error('blocked') },
  })
  await assert.rejects(work, /blocked/)
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(played, ['one'])
  assert.deepEqual(generated, ['one', 'two'])
}
// Browser audio denial offers a real-gesture retry and Stop invalidates even that retry.
{
  const instances = [], revoked = []
  const oldAudio = globalThis.Audio, oldCreate = URL.createObjectURL, oldRevoke = URL.revokeObjectURL
  class AudioFake {
    attempts = 0; paused = false; removed = false
    src = ''; loads = 0
    constructor() { instances.push(this) }
    async play() { if (++this.attempts === 1) throw new DOMException('Blocked', 'NotAllowedError') }
    pause() { this.paused = true }
    removeAttribute() { this.removed = true }
    load() { this.loads++ }
  }
  globalThis.Audio = AudioFake
  URL.createObjectURL = () => 'blob:fixture'
  URL.revokeObjectURL = url => revoked.push(url)
  try {
    const ready = deferred(), c = new AbortController()
    let resume
    const player = new AudioFake()
    const work = playSpeechAudio(new Blob(['wave']), c.signal, retry => { resume = retry; if (retry) ready.resolve() }, player)
    await ready.promise
    assert.equal(instances[0].attempts, 1)
    assert.equal(player.src, 'blob:fixture'); assert.equal(player.loads, 1)
    resume(); assert.equal(instances[0].attempts, 2)
    instances[0].onended(); await work
    assert.equal(resume, null); assert.ok(instances[0].paused && instances[0].removed)
    const blocked = deferred(), stop = new AbortController()
    const stopped = playSpeechAudio(new Blob(['wave']), stop.signal, retry => { if (retry) { resume = retry; blocked.resolve() } }, new AudioFake())
    await blocked.promise
    const staleRetry = resume
    stop.abort(); await assert.rejects(stopped, { name: 'AbortError' })
    staleRetry(); assert.equal(instances[1].attempts, 1)
    assert.ok(instances[1].paused && instances[1].removed); assert.equal(revoked.length, 2)
    const bad = new AudioFake()
    bad.play = async () => {}
    bad.error = { code: 3, message: 'Output device unavailable' }
    const failed = playSpeechAudio(new Blob(['wave']), new AbortController().signal, () => {}, bad)
    bad.onerror()
    await assert.rejects(failed, /media error 3: Output device unavailable/)
    assert.ok(bad.paused && bad.removed)
  } finally {
    globalThis.Audio = oldAudio; URL.createObjectURL = oldCreate; URL.revokeObjectURL = oldRevoke
  }
}
console.log('speech text, queue and audio: ok')
