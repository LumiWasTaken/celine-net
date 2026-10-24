/* Select: many rows of a list at once. A list's Select button (or a row menu's Select,
   a Ctrl or Cmd click, a Shift click for the rows between, a long press on a phone)
   starts it; then a click picks a row. The bar over the list says how many, has Select
   all, the actions that fit the list and Done. An action that destroys asks once,
   with the number. What the server could not do is said with its reason.

     const pick = usePicker(shelf)            // a new shelf starts with nothing picked
     pick.keys.current = rows.map(r => r.id)  // the rows in their order, at each render
     <ListRow pick={pick.row(id)} ... />
     <SelectBar picker={pick} noun={n => count(n, 'chat')} actions={[...]} /> */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { CheckCheck, type LucideIcon } from 'lucide-react'
import { Button } from './Button'
import { ConfirmDialog } from './Dialog'
import { InlineError } from './Notice'
import { useLongPress } from './phone'

/** what a row spreads (ListRow's `pick`) */
export type PickProps = {
  /** undefined while not selecting */
  checked: boolean | undefined
  /** a click: true when it picked (the row does nothing else) */
  onPick: (e: React.MouseEvent) => boolean
  /** a right-click right after a long press: true when it is swallowed */
  swallow: () => boolean
  onPointerDown: (e: React.PointerEvent) => void
  onPointerUp: () => void
  onPointerCancel: () => void
  onPointerLeave: () => void
}

export type Picker = {
  on: boolean
  picked: Set<string>
  /** the rows that can be picked, in their order; set at each render */
  keys: React.MutableRefObject<string[]>
  start: (key?: string) => void
  stop: () => void
  set: (keys: Iterable<string>) => void
  row: (key: string) => PickProps
}

export function usePicker(resetOn?: unknown): Picker {
  const [on, setOn] = useState(false)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const keys = useRef<string[]>([])
  const anchor = useRef<string | null>(null)
  const long = useLongPress()
  const pressed = useRef(false)
  useEffect(() => { setOn(false); setPicked(new Set()); anchor.current = null }, [resetOn])

  const start = (key?: string) => {
    setOn(true)
    setPicked(key ? new Set([key]) : new Set())
    anchor.current = key ?? null
  }
  const toggle = (key: string) => {
    setPicked(p => { const n = new Set(p); if (!n.delete(key)) n.add(key); return n })
    anchor.current = key
  }
  const range = (key: string) => {
    const list = keys.current
    const a = anchor.current ? list.indexOf(anchor.current) : -1
    const b = list.indexOf(key)
    if (a < 0 || b < 0) { toggle(key); return }
    setPicked(p => new Set([...p, ...list.slice(Math.min(a, b), Math.max(a, b) + 1)]))
  }
  const row = (key: string): PickProps => {
    const stopTimer = long.stop
    return {
      checked: on ? picked.has(key) : undefined,
      onPick: e => {
        if (pressed.current) { pressed.current = false; return true }
        if (e.button !== 0) return false
        if (e.shiftKey) { setOn(true); range(key); return true }
        if (e.ctrlKey || e.metaKey) { setOn(true); toggle(key); return true }
        if (on) { toggle(key); return true }
        return false
      },
      swallow: () => pressed.current,
      onPointerDown: e => {
        if (long.start(e, () => {
          pressed.current = true
          if (on) toggle(key); else start(key)
          navigator.vibrate?.(10)
        })) pressed.current = false
      },
      onPointerUp: stopTimer, onPointerCancel: stopTimer, onPointerLeave: stopTimer,
    }
  }
  return {
    on, picked, keys, start, row,
    stop: () => { setOn(false); setPicked(new Set()); anchor.current = null },
    set: k => setPicked(new Set(k)),
  }
}

/** what a bulk action did: the server's lists */
export type BulkResult = { done: string[]; skipped: { id: string; reason: string }[] }

export type BulkAction = {
  label: string
  icon: LucideIcon
  danger?: boolean
  /** asked once before it runs: the dialog's title and its button, for this many, and one
      Checkbox under it (off by default: "Also delete its sub-agents") */
  ask?: (n: number) => { title: string; confirm: string; body?: ReactNode; option?: string }
  /** `option`: that Checkbox's state */
  run: (keys: string[], option?: boolean) => Promise<BulkResult>
  /** the word for what was done, for "2 not archived" */
  done: string
}

export function SelectBar({ picker, noun, actions }: {
  picker: Picker
  /** "3 chats" */
  noun: (n: number) => string
  actions: BulkAction[]
}) {
  const [asking, setAsking] = useState<BulkAction | null>(null)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  useEffect(() => { if (!picker.on) setNote(null) }, [picker.on])
  if (!picker.on) return null
  const chosen = picker.keys.current.filter(k => picker.picked.has(k))
  const all = chosen.length === picker.keys.current.length && chosen.length > 0
  const run = async (a: BulkAction, option?: boolean) => {
    setBusy(true); setNote(null)
    try {
      const r = await a.run(chosen, option)
      if (r.skipped.length) {
        const why = [...new Set(r.skipped.map(s => s.reason))].join(' ')
        setNote(`${noun(r.skipped.length)} not ${a.done}: ${why}`)
        picker.set(r.skipped.map(s => s.id))
      } else picker.stop()
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }
  const asked = asking?.ask?.(chosen.length)
  return (
    <div className="select-bar" role="toolbar" aria-label="Selected">
      <div className="select-bar__row">
        <span className="select-bar__count">{chosen.length} selected</span>
        <Button size="sm" icon={CheckCheck} disabled={!picker.keys.current.length} pressed={all}
                onClick={() => picker.set(all ? [] : picker.keys.current)}>Select all</Button>
        <span className="select-bar__actions">
          {actions.map(a => (
            <Button key={a.label} size="sm" icon={a.icon}
                    disabled={!chosen.length || busy} title={a.label}
                    onClick={() => (a.ask ? setAsking(a) : void run(a))}>{a.label}</Button>
          ))}
          <Button size="sm" onClick={picker.stop}>Done</Button>
        </span>
      </div>
      {note && <InlineError compact message={note} />}
      {asking && asked && (
        <ConfirmDialog title={asked.title} body={asked.body} danger={asking.danger} confirmLabel={asked.confirm} option={asked.option}
                       onClose={() => setAsking(null)}
                       onConfirm={async option => { const a = asking; setAsking(null); await run(a, option) }} />
      )}
    </div>
  )
}
