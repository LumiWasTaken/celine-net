/* People: one row per person in every chat source. A same person is one row with its
   accounts inside it; every other account is a row of its own. Rename in place, write
   what someone is to you (About, Also called), mark accounts as the same person, and
   separate an account again. A row shows which account it is beyond the name (source,
   number or user name, last active): two people called Martin stay two people until
   you mark them. No column headers: each cell says what it is. */
import { useEffect, useState, type ReactNode } from 'react'
import { Link2, MoreHorizontal, NotebookPen, PenLine, SquareCheck, Unlink, Users } from 'lucide-react'
import { identitiesApi, kbApi, type Kb, type Person, type PersonAccount, type PersonEntry } from '../../lib/kb'
import { errorText, listTime, nothingMatches } from '../../lib/format'
import { useResource } from '../../lib/useResource'
import { useAutosave } from '../../lib/useAutosave'
import {
  Avatar, Button, Checkbox, Dialog, EmptyState, Field, InlineEdit, InlineError, Input, LoadingRow, MenuButton, SearchField,
  Section, Filters, SourceGlyph, VirtualList, useContextMenu, type MenuItem, TextArea
} from '../../ui'
import { Page } from './parts'
import './settings.css'

/* one line of the table in px; a same person has one more line per account */
const LINE = 44
const RENAME_ACCOUNT = 'Rename. The new name shows in every chat.'

export function People({ kbs }: { kbs: Kb[] }) {
  const [kbId, setKbId] = useState('')
  useEffect(() => { if (kbId && !kbs.some(k => k.id === kbId)) setKbId('') }, [kbs, kbId])
  const [q, setQ] = useState('')
  const [editing, setEditing] = useState<string | null>(null)
  const [about, setAbout] = useState<PersonEntry | null>(null)
  const [picked, setPicked] = useState<Map<string, PersonEntry>>(new Map())
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<{ key: string; message: string } | null>(null)
  const kbOf = (id: string) => kbs.find(k => k.id === id)
  const title = (id: string) => kbOf(id)?.title ?? id

  const people = useResource(() => identitiesApi.people({ q: q.trim() || undefined, kb: kbId || undefined }), [kbId, q],
                             { debounce: q ? 150 : 0, events: ev => ev.type === 'kb.changed' })
  const list = people.data?.people ?? []

  // a pick stays while the filter changes, and takes the newest data of its person
  useEffect(() => {
    const now = new Map((people.data?.people ?? []).map(p => [p.key, p]))
    setPicked(m => [...m].some(([k, v]) => now.has(k) && now.get(k) !== v)
      ? new Map([...m].map(([k, v]) => [k, now.get(k) ?? v])) : m)
  }, [people.data])

  const separate = async (a: PersonAccount) => {
    if (await act(a.ref, () => identitiesApi.unlink(a.ref))) setPicked(new Map())
  }
  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key); setError(null)
    try {
      await fn()
      people.reload()
      return true
    } catch (e) { setError({ key, message: errorText(e) }); return false } finally { setBusy(null) }
  }

  const toggle = (p: PersonEntry, on: boolean) => setPicked(m => {
    const next = new Map(m)
    if (on) next.set(p.key, p); else next.delete(p.key)
    return next
  })

  const markSame = async () => {
    const rows = [...picked.values()].sort((a, b) => b.messages - a.messages)
    // a same person keeps its name, else the busiest account gives it
    const name = (rows.find(r => r.same_person) ?? rows[0])!.name
    if (await act('mark', () => identitiesApi.link(rows.flatMap(r => r.accounts.map(a => a.ref)), name))) setPicked(new Map())
  }

  const renamePerson = (p: PersonEntry, name: string) => {
    setEditing(null)
    if (!name || name === p.name) return
    void act(p.key, () => identitiesApi.setPerson(p.accounts[0]!.ref, { name }))
  }
  const renameAccount = (a: PersonAccount, name: string) => {
    setEditing(null)
    if (name === (a.names?.custom ?? '')) return
    void act(a.ref, () => kbApi.patchPerson(a.kb, a.id, name || null))
  }

  const ctx = useContextMenu()
  const rowMenu = (p: PersonEntry): MenuItem[] => [
    { label: 'Rename', icon: PenLine, onSelect: () => setEditing(p.key) },
    { label: 'Edit about', icon: NotebookPen, onSelect: () => setAbout(p) },
    { label: picked.has(p.key) ? 'Clear selection' : 'Select', icon: SquareCheck, onSelect: () => toggle(p, !picked.has(p.key)) },
  ]
  const accountMenu = (a: PersonAccount): MenuItem[] => [
    { label: 'Rename account', icon: PenLine, onSelect: () => setEditing(a.ref) },
    { label: 'Separate account', icon: Unlink, disabled: busy === a.ref, onSelect: () => void separate(a) },
  ]
  const pickedList = [...picked.values()]

  /* the cells after the name: which account (or how many), what they are to you, last
     active, and the row's "..." (the same items as its right-click) */
  const cells = (what: string, a: PersonAccount | null, ts: number | null | undefined, about: ReactNode,
                 items: MenuItem[]) => (
    <>
      <span role="cell" className="people__source" title={a ? otherNames(a) || undefined : undefined}>
        {a && <SourceGlyph source={kbOf(a.kb)?.platform ?? a.kb} label={title(a.kb)} />}
        <span>{what}</span>
      </span>
      <span role="cell" className="people__about-cell">{about}</span>
      <span role="cell" className="people__when">{listTime(ts)}</span>
      <span role="cell" className="people__more">
        <MenuButton icon={MoreHorizontal} label="More" size="sm" items={items} />
      </span>
    </>
  )

  const nameCell = (key: string, name: string, seed: string | number, current: string, onSave: (t: string) => void,
                    hint: string, placeholder: string, avatar: boolean, extra?: ReactNode) => (
    <span role="cell" className="people__name">
      {avatar ? <Avatar name={name} seed={seed} size={28} /> : <span className="people__indent" aria-hidden />}
      {editing === key ? (
        <InlineEdit value={current} label={`Rename ${name}`} placeholder={placeholder}
                    onSave={onSave} onCancel={() => setEditing(null)} />
      ) : (
        <Button variant="ghost" size="sm" className="people__rename" title={hint} onClick={() => setEditing(key)}>{name}</Button>
      )}
      {extra}
      {error?.key === key && <InlineError message={error.message} compact />}
    </span>
  )

  const renderPerson = (p: PersonEntry) => {
    const one = p.same_person ? null : p.accounts[0]!
    const aka = p.aliases.length > 0 && <span className="people__aka" title={`Also called ${p.aliases.join(', ')}`}>{p.aliases.join(', ')}</span>
    return (
      <div role="rowgroup" className={`people__person${picked.has(p.key) ? ' is-picked' : ''}`}>
        <div role="row" className="people__row" onContextMenu={e => { if (editing !== p.key) ctx.open(e, rowMenu(p), p.name) }}
             {...(editing !== p.key ? ctx.press(() => rowMenu(p), p.name) : {})}>
          {/* the whole cell, the row's height, toggles the box */}
          <label role="cell" className="people__pick">
            <Checkbox checked={picked.has(p.key)} onChange={on => toggle(p, on)}
                      ariaLabel={`Select ${p.name}${one ? `, ${title(one.kb)}, ${one.handle ?? ''}` : ''}`} />
          </label>
          {one
            ? nameCell(p.key, p.name, one.id, one.names?.custom ?? p.name, t => renameAccount(one, t),
                       RENAME_ACCOUNT, 'Leave empty to use their own name', true, aka)
            : nameCell(p.key, p.name, p.key, p.name, t => renamePerson(p, t), 'Rename the person', 'Name', true, aka)}
          {cells(one ? one.handle ?? title(one.kb) : `${p.accounts.length} accounts`, one, one ? one.last_ts : p.last_ts,
                 p.about ? (
                   <Button variant="ghost" size="sm" className="people__about" title={p.about} onClick={() => setAbout(p)}>{p.about.split('\n')[0]}</Button>
                 ) : null, rowMenu(p))}
        </div>
        {!one && p.accounts.map(a => (
          <div key={a.ref} role="row" className="people__row people__row--account"
               onContextMenu={e => { if (editing !== a.ref) ctx.open(e, accountMenu(a), a.name) }}
               {...(editing !== a.ref ? ctx.press(() => accountMenu(a), a.name) : {})}>
            <span role="cell" />
            {nameCell(a.ref, a.name, a.id, a.names?.custom ?? a.name, t => renameAccount(a, t),
                      RENAME_ACCOUNT, 'Leave empty to use their own name', false)}
            {cells(a.handle ?? title(a.kb), a, a.last_ts, null, accountMenu(a))}
          </div>
        ))}
      </div>
    )
  }

  // nothing to list before a source has messages: one line, no tools over it
  if (!kbs.some(k => (k.messages ?? 0) > 0)) {
    return <Page title="People"><Section><EmptyState icon={Users} title="No people yet" /></Section></Page>
  }
  return (
    <Page title="People" wide>
      <Section className="people">
        <div className="people__bar">
          <SearchField className="people__search" value={q} onChange={setQ} loading={people.loading && !!q}
                       placeholder="Search" />
          {/* the sources, and the action once two are picked: one line whose height is kept, so nothing moves */}
          <div className="people__line">
            {kbs.length > 1 && (
              <Filters label="Source" value={kbId} onChange={setKbId}
                       options={[{ value: '', label: 'All sources' }, ...kbs.map(k => ({ value: k.id, label: k.title }))]} />
            )}
            {picked.size > 1 && (
              <Button variant="primary" icon={Link2} loading={busy === 'mark'} onClick={() => void markSame()}
                      title={`Search and the conversations treat these accounts as one person. Messages keep their own names.\n${pickedList.map(r => r.name).join(', ')}`}>
                Mark {picked.size} as the same person
              </Button>
            )}
          </div>
        </div>
        {error?.key === 'mark' && <InlineError message={error.message} />}
        {people.error && <InlineError message={people.error} onRetry={people.reload} />}
        <div className="people__table" role="table" aria-label="People">
          {!people.data && !people.error ? <LoadingRow label="Loading people" /> : list.length === 0 ? (
            <EmptyState compact>{q ? nothingMatches(q) : 'Nobody yet'}</EmptyState>
          ) : (
            <VirtualList items={list} getKey={p => p.key} page renderRow={renderPerson}
                         estimate={i => LINE * (list[i]!.same_person ? list[i]!.accounts.length + 1 : 1)} />
          )}
        </div>
        {ctx.menu}
      </Section>
      {about && <AboutDialog person={about} onClose={() => setAbout(null)} onSaved={people.reload} />}
    </Page>
  )
}

/* What someone is to you and the other names they go by. Celine reads both; search and
   Celine find the person by an alias. Each saves when you leave its field. */
function AboutDialog({ person, onClose, onSaved }: { person: PersonEntry; onClose: () => void; onSaved: () => void }) {
  const [text, setText] = useState(person.about)
  const [aliases, setAliases] = useState(person.aliases.join(', '))
  const auto = useAutosave<string>(async c => {
    await identitiesApi.setPerson(person.accounts[0]!.ref, {
      about: c.about ?? text, aliases: (c.aliases ?? aliases).split(',').map(a => a.trim()).filter(Boolean),
    })
    onSaved()
  })
  return (
    <Dialog title={`About ${person.name}`} onClose={onClose} size="md" actions={<Button onClick={onClose}>Close</Button>}>
      <div className="dialog__body people__about-form">
        <Field label="About">
          <TextArea rows={5} value={text} autoFocus maxLength={2000} onBlur={auto.flush}
                    placeholder="My sister. Birthday on 3 May." onChange={e => { setText(e.target.value); auto.set('about', e.target.value, 'blur') }} />
        </Field>
        <Field label="Also called">
          <Input value={aliases} placeholder="Sis, Annie" onBlur={auto.flush}
                 onChange={e => { setAliases(e.target.value); auto.set('aliases', e.target.value, 'blur') }} />
        </Field>
        {auto.error && <InlineError message={auto.error} compact onRetry={auto.flush} />}
      </div>
    </Dialog>
  )
}

function otherNames(p: Person): string {
  if (!p.names) return ''
  const n = p.names
  return [
    n.custom && `Your name for them: ${n.custom}`, n.contact && `Contacts: ${n.contact}`,
    n.push && `WhatsApp: ${n.push}`, n.display && `Discord: ${n.display}`, n.username && `User name: ${n.username}`,
  ].filter(Boolean).join('\n')
}
