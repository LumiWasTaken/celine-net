/* Text while it writes: what part of a reply is on screen. The network
   brings text in bursts; the screen shows it at a steady pace that follows the backlog,
   holding back about LAG seconds, in whole words. A reply that is already there when it
   mounts (history, a reload mid-answer) shows at once; reduced motion and a hidden tab
   show text as it arrives. The pure parts (the pace, the cut, the Markdown blocks and
   closing what is open) are exported for useReveal.check.mjs. */
import { useEffect, useRef, useState } from 'react'

export const LAG = 0.45       // s of text held back while it streams: bursts become one motion
export const END = 0.45       // s after the last token: the rest is on screen by then
export const MIN_CPS = 30     // never slower than this while text waits
export const TAU = 0.25       // s: how fast the pace follows the backlog

/** `left`: seconds until the end, from the last token on */
type Pace = { shown: number; rate: number; left?: number }

/** one frame: `dt` seconds later, how much of `length` characters shows */
export function step(p: Pace, length: number, live: boolean, dt: number): Pace {
  dt = Math.min(dt, 0.05)                        // a stalled frame never paints a burst
  const backlog = length - p.shown
  if (backlog <= 0) return { shown: length, rate: p.rate }
  if (live) {
    const target = Math.max(MIN_CPS, backlog / LAG)
    const rate = p.rate + (target - p.rate) * (1 - Math.exp(-dt / TAU))
    return { shown: Math.min(length, p.shown + rate * dt), rate }
  }
  // after the last token: an even speed-up that ends on time, the pace never drops
  const left = p.left ?? END
  if (left <= dt) return { shown: length, rate: p.rate }
  const speedUp = Math.max(0, 2 * (backlog - p.rate * left) / (left * left))
  const rate = p.rate + speedUp * dt
  return { shown: Math.min(length, p.shown + rate * dt), rate, left: left - dt }
}

const segmenter = typeof Intl !== 'undefined' && 'Segmenter' in Intl ? new Intl.Segmenter() : null

/** where to cut `text` at about `at` while it streams: after the last whole word when one
    ends within 24 characters, never inside a character (a surrogate pair, a combining mark) */
export function cutAt(text: string, at: number): number {
  if (at >= text.length) return text.length
  for (let i = at; i > at - 24 && i > 0; i--) if (/\s/.test(text[i - 1]!)) return i
  if (!segmenter) return at
  const from = Math.max(0, at - 16)
  let end = from
  for (const g of segmenter.segment(text.slice(from, at + 16))) {
    if (from + g.index > at) break
    end = from + g.index
  }
  return end
}

/** how much of a new text keeps what was on screen: the common prefix */
export function common(a: string, b: string): number {
  let i = 0
  while (i < a.length && i < b.length && a[i] === b[i]) i++
  return i
}

const reduced = () => typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches

/** the part of `text` on screen; `live` while tokens may still come */
export function useReveal(text: string, live: boolean): string {
  const pace = useRef<Pace>({ shown: text.length, rate: 0 })
  const was = useRef(text)
  const [, frame] = useState(0)
  // a rewrite that does not go on from what shows keeps the common prefix
  if (text !== was.current) {
    if (!text.startsWith(was.current.slice(0, Math.floor(pace.current.shown)))) {
      pace.current = { shown: Math.min(pace.current.shown, common(was.current, text)), rate: pace.current.rate }
    }
    was.current = text
  }
  const instant = reduced() || (typeof document !== 'undefined' && document.hidden)
  if (instant) pace.current = { shown: text.length, rate: 0 }
  const behind = pace.current.shown < text.length

  useEffect(() => {
    if (!behind) return
    let raf = 0
    let last = performance.now()
    const tick = (now: number) => {
      pace.current = step(pace.current, was.current.length, live, (now - last) / 1000)
      last = now
      frame(n => n + 1)
      if (pace.current.shown < was.current.length) raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [behind, live, text])

  const at = Math.floor(pace.current.shown)
  return text.slice(0, live && behind ? cutAt(text, at) : at)
}

/* ── Markdown while it streams ── */

/** the text in blocks at blank lines outside code fences; a blank line before an indented
    line (a list item going on) does not split */
export function blocks(text: string): string[] {
  const lines = text.split('\n')
  const out: string[] = []
  let cur: string[] = []
  let fence = false
  lines.forEach((line, i) => {
    if (/^\s*(```|~~~)/.test(line)) fence = !fence
    const next = lines.slice(i + 1).find(l => l.trim())
    if (!fence && !line.trim() && cur.length && next !== undefined && !/^\s/.test(next)) {
      out.push(cur.join('\n'))
      cur = []
      return
    }
    cur.push(line)
  })
  if (cur.length) out.push(cur.join('\n'))
  return out
}

/** the last block of a reply being written, its open markers closed, so nothing flips from
    literal to formatted: a fence, **, *, _, `, ~~; an unfinished link shows as its text, an
    unfinished image or reference is left out until it is whole */
export function closeOpen(block: string): string {
  const fences = block.split('\n').filter(l => /^\s*(```|~~~)/.test(l)).length
  if (fences % 2) return `${block}\n\`\`\``
  let s = block.replace(/!\[[^\]\n]*(\]\([^)\n]*)?$/, '')     // an image on its way
    .replace(/\[\[[^\]\n]*\]?$/, '')                         // a reference on its way
    .replace(/\[([^\]\n]*)\]\([^)\n]*$/, '$1')                 // a link on its way: its text
    .replace(/\[([^\]\n]*)$/, '$1')
    .replace(/\s+$/, '')
  const code = (s.match(/`/g) ?? []).length % 2 === 1
  if (code) return `${s}\``
  const bare = s.replace(/`[^`]*`/g, '')
  const closers: string[] = []
  if ((bare.match(/\*\*/g) ?? []).length % 2) closers.push('**')
  if ((bare.match(/~~/g) ?? []).length % 2) closers.push('~~')
  const single = bare.replace(/\*\*|~~/g, '  ')
  // an opener follows a space or the start and comes before a non-space (a list's "* " is
  // none); a closer follows a non-space and comes before a space or the end
  for (const mark of ['*', '_']) {
    let open = false
    for (let i = 0; i < single.length; i++) {
      if (single[i] !== mark) continue
      const before = single[i - 1] ?? ' ', after = single[i + 1] ?? ' '
      if (open && /\S/.test(before) && !/\w/.test(after)) open = false
      else if (!open && !/\w/.test(before) && /\S/.test(after)) open = true
    }
    if (open) closers.push(mark)
  }
  return s + closers.reverse().join('')
}
