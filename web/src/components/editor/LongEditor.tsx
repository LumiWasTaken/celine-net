/* The one large editor for every long text (its persona and example lines, Banned words,
   notes, a Routine's prompts, lyrics): a page of its own
   inside the caller's place. The text sits on a writing surface in the story font and takes the
   page's height; the fields that belong to it sit above it (`top`) or after it (`after`);
   Cancel and Save at its foot, or with `autosave` (its persona, example lines and Banned
   words) no buttons: the text saves when you leave its box or the page, and after Insert
   or Replace, so its History gains one entry per save, never one per keystroke. History is its own page (the caller's route, TextHistory).
   Celine's box sits beside the text on a wide screen and above it on any other: ask it, its
   draft streams in, and only Copy, Insert or Replace puts it into the text. */
import { useRef, useState, type ReactNode } from 'react'
import { History } from 'lucide-react'
import { Button, Field, InlineError, PlaceHead, TextArea, type Crumb, type MenuItem } from '../../ui'
import type { TextOf } from '../../lib/texts'
import { errorText } from '../../lib/format'
import { useAutosave } from '../../lib/useAutosave'
import { AskCeline } from './AskCeline'
import './editor.css'

/** one long text of the editor: its key (what Save returns it under), label, start value and
    what it is for Celine's drafts (`of`, else the editor's) */
export type EditorText = { key: string; label?: string; value: string; of?: TextOf }

export function LongEditor({ crumbs, title, of, history, more = [], top, texts, after, hint, onSave, onCancel, dirty: outside = false, autosave = false }: {
  crumbs: Crumb[]
  title: string
  /** what the texts are (History, Ask Celine); a new item has none yet */
  of?: TextOf
  /** the History page of this item, the caller's route; a new item has none */
  history?: string
  more?: MenuItem[]
  /** the fields above the texts (Name, Keywords, a portrait) */
  top?: ReactNode
  /** one text fills the page; several stack, each growing with its text */
  texts: EditorText[]
  /** the fields after the texts */
  after?: ReactNode
  /** an example of what to ask it, in its box */
  hint: string
  /** the texts by key; a rejection's sentence shows at the foot and nothing is lost */
  onSave: (values: Record<string, string>) => Promise<void>
  /** Cancel; none with `autosave` */
  onCancel?: () => void
  /** the caller's own fields changed (Save is needed even when no text changed) */
  dirty?: boolean
  /** no Save or Cancel: each text saves on blur, on Insert or Replace, and when the page is left */
  autosave?: boolean
}) {
  const [vals, setVals] = useState(() => Object.fromEntries(texts.map(t => [t.key, t.value])))
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [target, setTarget] = useState(texts[0]?.key ?? '')
  const caret = useRef<Record<string, number>>({})
  const one = texts.length === 1
  const text = texts.find(t => t.key === target)
  const auto = useAutosave<string>(changes => onSave({ ...vals, ...changes }))
  const put = (k: string, v: string, when: 'now' | 'blur' = 'blur') => {
    setVals(o => ({ ...o, [k]: v }))
    if (autosave) auto.set(k, v, when)
    else setDirty(true)
  }
  const insert = (draft: string) => {
    const v = vals[target] ?? '', at = caret.current[target] ?? v.length
    const before = v.slice(0, at).replace(/\s*$/, ''), rest = v.slice(at).replace(/^\s*/, '')
    put(target, [before, draft, rest].filter(Boolean).join('\n\n'), 'now')
  }
  const save = async () => {
    setSaving(true); setError(null)
    try { await onSave(vals); setDirty(false) } catch (e) { setError(errorText(e)) } finally { setSaving(false) }
  }

  return (
    <div className="page page--fill led">
      <PlaceHead level={1} crumbs={crumbs} title={title}
                 actions={history && <Button icon={History} href={history}>History</Button>} more={more} />
      <div className="led-body">
        <div className="led-main">
          <div className="led-scroll">
            <div className={one ? 'led-sheet led-sheet--one' : 'led-sheet'}>
              {top && <div className="led-top">{top}</div>}
              {texts.map(t => (
                <Field key={t.key} label={t.label} className={one ? 'led-field led-field--fill' : 'led-field'}>
                  <TextArea fill className="led-text" value={vals[t.key]} aria-label={t.label ?? 'Text'} spellCheck
                            onFocus={() => setTarget(t.key)} onBlur={autosave ? auto.flush : undefined}
                            onSelect={e => { caret.current[t.key] = e.currentTarget.selectionStart }}
                            onChange={e => put(t.key, e.target.value)} />
                </Field>
              ))}
              {after}
            </div>
          </div>
          {autosave ? auto.error && <InlineError compact message={auto.error} onRetry={auto.flush} /> : (
            <div className="led-foot">
              <span className="led-unsaved" role={error ? 'alert' : undefined}>{error ?? (dirty || outside ? 'Unsaved changes' : '')}</span>
              <Button onClick={onCancel}>Cancel</Button>
              <Button variant="primary" loading={saving} disabled={!dirty && !outside} onClick={() => void save()}>Save</Button>
            </div>
          )}
        </div>
        <AskCeline of={text?.of ?? of} text={vals[target] ?? ''} label={texts.length > 1 ? text?.label : undefined} hint={hint}
                   onInsert={insert} onReplace={d => put(target, d, 'now')} />
      </div>
    </div>
  )
}
