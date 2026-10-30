/* Times, counts and names.
   One English locale for everything, whatever the browser is set to: en-GB gives
   "12 Mar 2021", a 24-hour clock and "23,571". Never pass `undefined` as a locale. */

export const LOCALE = 'en-GB'
const DAY = 86_400_000

/* en-GB spells September "Sept" in short form; the app says "Sep" like every other month */
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "28 Sep", or "28 Sep 2025" with the year. */
export function shortDate(ms: number, year = false): string {
  const d = new Date(ms)
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${year ? ` ${d.getFullYear()}` : ''}`
}

const startOfDay = (t: number) => {
  const d = new Date(t)
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
}

/* THE time rule, two functions:
     listTime   a row in a list, relative and bare: "now", "12m", "19:00" today,
                "Yesterday", "Fri" this week, "9 Oct", "9 Oct 2025"
     stamp      anywhere else (a message, a head, a sentence): "19:00" today,
                "Fri 9 Oct, 19:00" otherwise, the year added when it is not this one
   fullDate is only for tooltips; dueTime (later) says a time in a sentence. */

const toMs = (t: number | string | null | undefined) => (typeof t === 'string' ? Date.parse(t) : t ?? NaN)

export function listTime(t: number | string | null | undefined, now = Date.now()): string {
  const ms = toMs(t)
  if (!ms || Number.isNaN(ms)) return ''
  const diff = now - ms
  if (diff >= 0 && diff < 60_000) return 'now'
  if (diff >= 0 && diff < 3_600_000) return `${Math.floor(diff / 60_000)}m`
  const days = Math.round((startOfDay(now) - startOfDay(ms)) / DAY)
  if (days === 0) return clock(ms)
  if (days === 1) return 'Yesterday'
  const d = new Date(ms)
  if (days > 1 && days < 7) return d.toLocaleDateString(LOCALE, { weekday: 'short' })
  return shortDate(ms, d.getFullYear() !== new Date(now).getFullYear())
}

export function stamp(t: number | string | null | undefined, now = Date.now()): string {
  const ms = toMs(t)
  if (!ms || Number.isNaN(ms)) return ''
  if (sameDay(ms, now)) return clock(ms)
  const d = new Date(ms)
  return `${d.toLocaleDateString(LOCALE, { weekday: 'short' })} ${shortDate(ms, d.getFullYear() !== new Date(now).getFullYear())}, ${clock(ms)}`
}

/** how long something took or has run: "52s", "3m 12s", "1h 4m" */
export function duration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

/** "1:42" */
export function countdown(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export function clock(ms: number): string {
  return new Date(ms).toLocaleTimeString(LOCALE, { hour: '2-digit', minute: '2-digit', hour12: false })
}

/** "12 KB", "3.4 MB". */
export function bytes(n: number): string {
  if (n < 1024) return count(n, 'byte')
  const units = ['KB', 'MB', 'GB', 'TB']
  let v = n / 1024, i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v < 10 ? v.toFixed(1) : num(Math.round(v))} ${units[i]}`
}

/** "Tue 12 March 2021, 14:03" (tooltips). */
export function fullDate(ms: number | string): string {
  const d = new Date(ms)
  const day = d.toLocaleDateString(LOCALE, { weekday: 'short', day: 'numeric', month: 'long', year: 'numeric' })
  return `${day.replace(',', '')}, ${clock(d.getTime())}`
}

/** Day separator label: Today, Yesterday, "Tuesday 12 March", "12 March 2021". */
export function dayLabel(ms: number, now = Date.now()): string {
  const days = Math.round((startOfDay(now) - startOfDay(ms)) / DAY)
  if (days === 0) return 'Today'
  if (days === 1) return 'Yesterday'
  const d = new Date(ms)
  if (days > 1 && days < 7) return d.toLocaleDateString(LOCALE, { weekday: 'long' })
  const sameYear = d.getFullYear() === new Date(now).getFullYear()
  return d.toLocaleDateString(LOCALE, sameYear
    ? { weekday: 'long', day: 'numeric', month: 'long' }
    : { day: 'numeric', month: 'long', year: 'numeric' })
}

export const sameDay = (a: number, b: number) => startOfDay(a) === startOfDay(b)

export function monthYear(ms: number | null | undefined): string {
  if (!ms) return ''
  const d = new Date(ms)
  return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`
}

const nf = new Intl.NumberFormat(LOCALE)
/** a filter that found nothing, the same words in every list: Nothing matches “words” */
export const nothingMatches = (q: string) => `Nothing matches “${q.trim()}”`

export const num = (n: number | null | undefined) => nf.format(n ?? 0)

/** "1 note", "12 notes". Singular spelled out, never "note(s)". */
export function count(n: number | null | undefined, one: string, many = `${one}s`): string {
  const v = n ?? 0
  return `${nf.format(v)} ${v === 1 ? one : many}`
}

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/** when something lands later, after "lands": "22:04" today, "tomorrow 08:00", else the stamp */
export function dueTime(t: number | string | null | undefined, now = Date.now()): string {
  const ms = toMs(t)
  if (!ms || Number.isNaN(ms)) return ''
  const days = Math.round((startOfDay(ms) - startOfDay(now)) / DAY)
  if (days === 0) return clock(ms)
  if (days === 1) return `tomorrow ${clock(ms)}`
  return stamp(ms, now)
}

const TURN_STEP: Record<string, string> = {
  queued: 'Waiting for the model', preparing: 'Answering', streaming: 'Answering', tools: 'Using a tool',
}

/** a turn at work, in words, the same on every page */
export const turnStep = (state: string | null | undefined): string | null =>
  (state && TURN_STEP[state]) || null
