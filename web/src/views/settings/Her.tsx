/* Settings, its page (/settings/agent): one column. The agent's picture (a click shows it big; its
   "..." has Upload and Delete), Name, Pronouns, Colour; Persona, Example lines, each a
   long text on a surface that opens in the large editor (HerText below: History and Ask
   Celine, keys her:persona, her:talk and her:voice; Restore this version saves at once); Banned words with Bias and its switch.
   Everything saves itself (lib/useAutosave): switches and choices at once, Bias when the
   slider rests, Name when you leave the field, a long text when you leave its editor or
   its box (one History entry per save). Every save sends the version its page was loaded
   at: a change made meanwhile is the server's sentence with Reload, your words kept.
   API: lib/settingsApi.ts herApi (GET/PUT /her). */
import { useState } from 'react'
import { MoreHorizontal, Pencil, SearchX, Trash2, Upload } from 'lucide-react'
import { LongEditor, TextHistory, textHistory } from '../../components/editor'
import { ApiError } from '../../lib/api'
import { COLORS, colorOf } from '../../lib/her'
import { format, useNav, type Route } from '../../lib/nav'
import { herApi, type HerPut, type HerView, type Voice } from '../../lib/settingsApi'
import { textsApi } from '../../lib/texts'
import { useOnline } from '../../lib/socket'
import { useAction } from '../../lib/useAction'
import { useAutosave, type Autosave } from '../../lib/useAutosave'
import { useResource } from '../../lib/useResource'
import {
  Button, Choice, EmptyState, Face, Field, InlineError, Input, Loaded, MenuButton, Notice, Section, Slider, Switch, Viewer, type MenuItem,
} from '../../ui'
import { GeneralFields } from './General'
import { Page } from './parts'
import './settings.css'

export const PRONOUNS = [{ value: 'she', label: 'She' }, { value: 'he', label: 'He' }, { value: 'they', label: 'They' }]

const herPage: Route = { section: 'settings', page: 'agent' }
const editRoute = (key: string, history = false): Route => ({ section: 'settings', page: 'agent', edit: key, ...(history && { history }) })

/** it and how to save it: with the version it was loaded at; a conflict is kept as the
    server's sentence until Reload */
function useHerView() {
  const res = useResource(() => herApi.get(), [], { events: ev => ev.type === 'her.changed' })
  const [conflict, setConflict] = useState<string | null>(null)
  const put = async (body: HerPut) => {
    try {
      const next = await herApi.put(body, res.data!.version)
      setConflict(null)
      res.set(next)
      return next
    } catch (e) {
      if (e instanceof ApiError && e.code === 'version_conflict') setConflict(e.message)
      throw e
    }
  }
  const reload = () => { setConflict(null); res.reload() }
  return { res, her: res.data ?? null, put, conflict, reload }
}

const Conflict = ({ text, onReload }: { text: string | null; onReload: () => void }) =>
  text ? <Notice tone="error" compact action={{ label: 'Reload', onClick: onReload }}>{text}</Notice> : null

export function HerPage() {
  const { res, her, put, conflict, reload } = useHerView()
  // name, pronouns, color, on and bias, in one PUT; its voice whole with what changed
  const auto = useAutosave(async c => {
    if (!her) return
    const { on, bias, ...rest } = c as Partial<HerPut & { on: boolean; bias: number }>
    await put({ ...rest, ...((on !== undefined || bias !== undefined) && { voice: { ...her.voice, ...(on !== undefined && { on }), ...(bias !== undefined && { bias }) } }) })
  })
  if (!her) {
    return (
      <Page title="">
        {<Loaded res={res} />}
      </Page>
    )
  }
  return (
    <Page title={her.name}>
      {conflict ? <Section><Conflict text={conflict} onReload={reload} /></Section>
        : auto.error && <Section><InlineError compact message={auto.error} onRetry={auto.flush} /></Section>}
      <Who her={her} auto={auto} />
      <Persona her={her} />
      <Section><GeneralFields only={['ask_before_setup']} /></Section>
      <Banned her={her} auto={auto} />
    </Page>
  )
}

/* a line break inside a paragraph joins (prose stored wrapped reads as one paragraph);
   a blank line, a list item or a heading keeps its break */
const reflow = (text: string) => text.replace(/([^\n])\n(?=[^\n\-*#>\d])/g, '$1 ')

/* a long text on a surface; a click opens it in the large editor. `lines`: each line
   stands alone (its example lines), else it reads as prose */
export function TextPreview({ label, text, to, mono = false, lines = false, empty }: { label?: string; text: string; to: Route; mono?: boolean; lines?: boolean; empty: string }) {
  const nav = useNav()
  const href = format(to)
  return (
    <div className="her-text">
      {label && <span className="her-label">{label}</span>}
      <a className={mono ? 'her-preview her-preview--mono' : 'her-preview'} href={href} title="Edit"
         onClick={e => { if (e.button === 0 && !e.metaKey && !e.ctrlKey) { e.preventDefault(); nav.go(to) } }}>
        {text.trim() ? (mono || lines ? text : reflow(text)) : <span className="her-preview__empty">{empty}</span>}
      </a>
    </div>
  )
}

/* ── its head: its picture whole (a click shows it big; its "..." has Upload and Delete),
   Name, Pronouns, Colour ── */
function Who({ her, auto }: { her: HerView; auto: Autosave<unknown> }) {
  const online = useOnline()
  const p = auto.pending as Partial<HerView>
  const [big, setBig] = useState(false)
  const upload = useAction(async (f: File) => { await herApi.putPortrait(f) })
  const remove = useAction(async () => { await herApi.deletePortrait() })
  const pick = () => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = 'image/png,image/jpeg,image/webp'
    input.onchange = () => { const f = input.files?.[0]; if (f) void upload.run(f) }
    input.click()
  }
  const menu: MenuItem[] = [
    { label: 'Upload', icon: Upload, disabled: !online, onSelect: pick },
    ...(her.portrait ? ['sep' as const, { label: 'Delete', icon: Trash2, danger: true, disabled: !online, onSelect: () => void remove.run() }] : []),
  ]
  const color = colorOf(her)
  return (
    <Section>
      <div className="her-pic">
        <div className="her-pic__frame" role="button" tabIndex={0} title={her.portrait ? 'Show the picture' : 'Upload'}
             style={{ background: `color-mix(in oklch, ${color} 22%, var(--surface-2))` }} onClick={her.portrait ? () => setBig(true) : pick}
             onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (her.portrait) setBig(true); else pick() } }}>
          {her.portrait ? <img className="her-pic__img" src={her.portrait.url} alt={her.name} draggable={false} />
            : <Face name={her.name} size="lg" still bg={`color-mix(in oklch, ${color} 22%, var(--surface-2))`} fg={color} />}
        </div>
        <span className="her-pic__more"><MenuButton icon={MoreHorizontal} size="sm" label={`More for ${her.name}`} items={menu} /></span>
      </div>
      {big && her.portrait && <Viewer src={her.portrait.url} name={her.name} onClose={() => setBig(false)} />}
      <Field label="Name">
        <Input value={p.name ?? her.name} maxLength={40} onChange={e => auto.set('name', e.target.value, 'blur')} onBlur={auto.flush}
               onKeyDown={e => { if (e.key === 'Enter') auto.flush() }} />
      </Field>
      <Field label="Pronouns" group>
        <Choice label="Pronouns" value={p.pronouns ?? her.pronouns} options={PRONOUNS} disabled={!online} onChange={v => auto.set('pronouns', v)} />
      </Field>
      <Field label="Colour" group>
        <Choice label="Colour" value={p.color ?? her.color} disabled={!online} onChange={v => auto.set('color', v)}
                options={COLORS.map(k => ({ value: k, label: k[0]!.toUpperCase() + k.slice(1), swatch: `var(--char-${k})` }))} />
      </Field>
      {(upload.error || remove.error) && <InlineError compact message={upload.error ?? remove.error} />}
    </Section>
  )
}

/* ── Persona: its text as long as it is; its example lines, a section of their own ── */
function Persona({ her }: { her: HerView }) {
  return (
    <>
      <Section title="Persona" action={<Button icon={Pencil} href={format(editRoute('persona'))}>Edit</Button>}>
        <TextPreview text={her.persona} to={editRoute('persona')} empty="Persona" />
      </Section>
      <Section title="Example lines" action={<Button icon={Pencil} href={format(editRoute('talk'))}>Edit</Button>}>
        <TextPreview lines text={her.talk ?? her.talk_shipped} to={editRoute('talk')} empty="Example lines" />
      </Section>
    </>
  )
}

/* ── Banned words: the one list its models are steered from, on or off; Bias under it ── */
function Banned({ her, auto }: { her: HerView; auto: Autosave<unknown> }) {
  const online = useOnline()
  const p = auto.pending as { on?: boolean; bias?: number }
  const v = her.voice
  const on = p.on ?? v.on
  return (
    <Section title="Banned words" count={v.banned.length}
             action={<span title="Banned words and phrases work on tabbyAPI, llama-swap and llama-server models"><Switch hideLabel label="Banned words" checked={on} disabled={!online} onChange={x => auto.set('on', x)} /></span>}>
      <TextPreview mono text={v.banned.map(w => w.trim()).join(', ')} to={editRoute('banned')} empty="Banned words" />
      <Slider label={<span title="How strongly to avoid them; -100 bans">Bias</span>} value={p.bias ?? v.bias}
              min={-100} max={0} step={1} disabled={!on} onChange={n => auto.set('bias', n, 'soon')} />
    </Section>
  )
}

/* ── one of its long texts in the large editor, or its History ── */

/** `current`: the text as its History keeps it, when the editor shows another form (the
    Banned words alone of its voice) */
type Opened = { title: string; of: string; text: string; current?: string; hint: string; save: (text: string) => Promise<unknown>; more?: MenuItem[] }

export function HerText({ edit, history }: { edit: string; history: boolean }) {
  const nav = useNav()
  const { res, her, put, conflict, reload } = useHerView()
  const back = () => nav.go(herPage)
  if (!her) return <Page title="">{<Loaded res={res} />}</Page>
  const voice = (v: Partial<Voice>) => put({ voice: { ...her.voice, ...v } })
  const words = (t: string) => t.split('\n').map(w => w.replace(/\r$/, '')).filter(w => w.trim())
  const opened: Opened | null =
    edit === 'persona' ? { title: 'Persona', of: 'her:persona', text: her.persona, hint: 'Warmer with strangers', save: t => put({ persona: t }) }
    : edit === 'talk' ? { title: 'Example lines', of: 'her:talk', text: her.talk ?? her.talk_shipped, hint: 'Add three more lines',
                          save: t => put({ talk: t }),
                          more: her.talk !== null ? [{ label: 'Use the default', onSelect: () => void put({ talk: null }).then(back) }] : [] }
    : edit === 'banned' ? { title: 'Banned words', of: 'her:voice', text: her.voice.banned.join('\n'), current: her.voice_text, hint: 'Add ten more',
                            save: t => voice({ banned: words(t) }) }
    : null
  if (!opened) return <Page title=""><EmptyState icon={SearchX} title="Not found">No text at this link.</EmptyState></Page>
  const crumbs = [{ label: 'Settings', href: format({ section: 'settings' }) }, { label: her.name, href: format(herPage) }]
  if (history) {
    // Restore this version saves it at once (POST /text-versions/{id}/revert), then the editor
    return (
      <TextHistory crumbs={[...crumbs, { label: opened.title, href: format(editRoute(edit)) }]} source={textHistory(opened.of)}
                   current={opened.current ?? opened.text} onRestore={(_, id) => textsApi.revert(id).then(() => { reload(); nav.go(editRoute(edit)) })} />
    )
  }
  return (
    <LongEditor crumbs={crumbs} title={opened.title} of={opened.of} history={format(editRoute(edit, true))}
                more={opened.more} hint={opened.hint}
                top={conflict && <Conflict text={conflict} onReload={reload} />}
                texts={[{ key: 'text', label: opened.title, value: opened.text }]}
                autosave onSave={async v => { await opened.save(v.text ?? '') }} />
  )
}
