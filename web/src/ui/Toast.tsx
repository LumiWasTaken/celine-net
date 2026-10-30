/* Toast: a short result of something the user did that has no other place to show
   ("Created Mira 2", "Copied"), or a running import with its progress. One ToastHost in
   App.tsx shows at most three, newest at the bottom; a plain one goes after 5 s, one with
   an action after 10 s, one with progress when it is done. Never an error (errors sit where
   they happened), never Undo for a delete or a move to Trash (those ask first).

     const t = toast({ text: 'Importing 3 files', progress: 0 })
     t.update({ progress: 0.5 }); t.close() */
import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { Progress } from './Bits'
import { Button, IconButton } from './Button'
import { leave, OUT, useLeave, useMove } from './motion'

/** how long a toast stays: 5 s, 10 s with an action */
const TOAST_MS = 5000
const TOAST_ACTION_MS = 10_000

type ToastSpec = {
  text: string
  /** a notification arriving: who or what it is from (a Face, a glyph) */
  lead?: ReactNode
  /** its first line, above `text` (the sender's name, "Vault") */
  title?: string
  /** one action ("Open", "Undo" of a creation) */
  action?: { label: string; onClick: () => void }
  /** 0 to 1; null while the end is not known. A toast with progress stays until closed */
  progress?: number | null
}
type Shown = ToastSpec & { id: number }

let shown: Shown[] = []
let next = 1
const subs = new Set<() => void>()
const emit = () => { for (const f of subs) f() }
const MAX = 3

function close(id: number) { shown = shown.filter(t => t.id !== id); emit() }

/** show a toast; returns a handle to change or close it */
export function toast(spec: ToastSpec) {
  const id = next++
  shown = [...shown, { ...spec, id }].slice(-MAX)
  emit()
  return {
    update: (s: Partial<ToastSpec>) => { shown = shown.map(t => (t.id === id ? { ...t, ...s } : t)); emit() },
    close: () => close(id),
  }
}

const read = () => shown
const subscribe = (f: () => void) => { subs.add(f); return () => { subs.delete(f) } }

/** the one host, in App.tsx; it stays when empty, so the last toast has a place to leave from */
export function ToastHost() {
  const list = useSyncExternalStore(subscribe, read)
  // the stack glides when one comes or goes; each toast rises and drops by itself
  const box = useRef<HTMLDivElement>(null)
  useMove(box, list, { bottom: true })
  return createPortal(
    <div ref={box} className="toasts" role="status" aria-live="polite">
      {list.map(t => <Toast key={t.id} {...t} onClose={() => close(t.id)} />)}
    </div>,
    document.body,
  )
}

/** one toast; the host draws them, /parts draws one in place */
export function Toast({ text, lead, title, action, progress, onClose }: ToastSpec & { onClose: () => void }) {
  const box = useRef<HTMLDivElement>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useLeave(box, g => leave(g, OUT.drop))
  const lasting = progress !== undefined
  useEffect(() => {
    if (lasting) return
    const id = window.setTimeout(() => closeRef.current(), action ? TOAST_ACTION_MS : TOAST_MS)
    return () => window.clearTimeout(id)
  }, [action, lasting])
  return (
    <div ref={box} className="toast">
      <div className="toast__row">
        {lead && <span className="toast__lead">{lead}</span>}
        {title ? <span className="toast__text"><b className="toast__title">{title}</b><span className="toast__line">{text}</span></span>
          : <span className="toast__text">{text}</span>}
        {action && <Button size="sm" variant="ghost" onClick={() => { closeRef.current(); action.onClick() }}>{action.label}</Button>}
        <IconButton icon={X} label="Close" size="sm" onClick={() => closeRef.current()} />
      </div>
      {lasting && <Progress value={progress ?? undefined} max={progress == null ? null : 1} label={text} />}
    </div>
  )
}

/** text onto the clipboard: the Clipboard API, or a hidden text box where the browser has
    none (plain http) or refuses it; true when it worked */
export async function copy(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true } catch { /* the old way, below */ }
  const box = document.createElement('textarea')
  box.value = text
  box.setAttribute('readonly', '')
  box.style.position = 'fixed'
  box.style.opacity = '0'
  document.body.append(box)
  box.select()
  try { return document.execCommand('copy') } catch { return false } finally { box.remove() }
}

/** a button that says Copied itself: `copied` for a moment after `copyIt(text)` copied */
export function useCopied(): [boolean, (text: string) => Promise<void>] {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const t = window.setTimeout(() => setCopied(false), COPIED_MS)
    return () => window.clearTimeout(t)
  }, [copied])
  return [copied, async (text: string) => { if (await copy(text)) setCopied(true) }]
}
const COPIED_MS = 1400

/** copy, and say "Copied" (a menu's Copy, which has no button to say it) */
export const copied = (text: string) => void copy(text).then(ok => { if (ok) toast({ text: 'Copied' }) })
