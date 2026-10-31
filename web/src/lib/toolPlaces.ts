/* Where each call of a step shows in the stream (components/ToolRow.tsx ToolLines). */

/** a call at work: pending, running or handed to a job */
export const busy = (call: { state: string }) =>
  call.state === 'pending' || call.state === 'running' || call.state === 'detached'

/** loading a skill, reading a guide: plumbing */
export const PLUMBING = new Set(['skills__load', 'skills__read_guide'])

/** The calls of a step as `lines` (one line each, under its words) and `rows` (its list).
    `inline`: the list is drawn. `ownBlock`: a settled call whose block shows it already (a
    gallery, a page's card). `runHere`: null for a call in no run, else whether its run's row
    stands at this call (a run's other calls are in that row).
    One call is in one place: with the list drawn, every settled call that is not plumbing
    and has no block of its own is a row, whatever it started still works (a sub-agent, a
    message that waits: its row shows that, with its Stop or Cancel), and has no line. */
export function placeCalls<C extends { name: string; state: string }>(
  calls: C[], inline: boolean, ownBlock: (c: C) => boolean, runHere: (c: C) => boolean | null,
): { lines: C[]; rows: C[] } {
  const listed = (c: C) => inline && !busy(c) && !PLUMBING.has(c.name) && !ownBlock(c)
  return {
    lines: calls.filter(c => !listed(c)),
    rows: calls.filter(c => listed(c) && runHere(c) !== false),
  }
}
