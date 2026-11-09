/* Settings, Advanced, Prompts (/settings/prompts[/edit/<id>[/history]]): every text a model reads,
   in the server's groups (its name: rows that open its persona, talk and banned words on
   its page; App; Skill guides; Guard: Bastet's voice on its page, then the judges). A row
   opens the text in the large editor, saved as you leave it; "Edited" marks one the user
   changed. History is its own page (the History button); Use the default is in its "...".
   API: lib/settingsApi.ts promptsApi (server/celine/api/prompts.py). */
import { useState } from 'react'
import { RotateCcw } from 'lucide-react'
import { LongEditor, TextHistory, textHistory } from '../../components/editor'
import { format, useNav } from '../../lib/nav'
import { promptsApi } from '../../lib/settingsApi'
import { useResource } from '../../lib/useResource'
import { ListRow, Loaded, Section, StateWord, type MenuItem } from '../../ui'
import { Page } from './parts'
import './settings.css'

const list = { section: 'settings', page: 'prompts' } as const
const up = { page: 'advanced', label: 'Advanced' }

export function Prompts({ id, history = false }: { id?: string; history?: boolean }) {
  return id ? <PromptText id={id} history={history} /> : <PromptList />
}

function PromptList() {
  const res = useResource(() => promptsApi.list(), [], { events: ev => ev.type === 'settings.changed' })
  return (
    <Page title="Prompts" help={res.data?.help} up={up}>
      {!res.data ? <Section><Loaded res={res} /></Section> : res.data.groups.map(g => (
        <Section key={g.label} title={g.label} count={g.items.length}>
          <div className="rows">
            {g.items.map(i => (
              <ListRow key={i.id} title={i.name} subtitle={i.about || undefined}
                       href={i.href ?? format({ ...list, edit: i.id })}
                       meta={i.edited ? <StateWord>Edited</StateWord> : undefined} />
            ))}
          </div>
        </Section>
      ))}
    </Page>
  )
}

function PromptText({ id, history }: { id: string; history: boolean }) {
  const nav = useNav()
  const res = useResource(() => promptsApi.get(id), [id])
  // a new text from the server (Use the default) starts the editor anew
  const [round, setRound] = useState(0)
  const p = res.data
  if (!p) return <Page title="Prompts" up={{ page: 'prompts', label: 'Prompts' }}><Section><Loaded res={res} /></Section></Page>
  const crumbs = [{ label: 'Settings', href: format({ section: 'settings' }) },
                  { label: 'Advanced', href: format({ section: 'settings', page: 'advanced' }) },
                  { label: 'Prompts', href: format(list) }]
  const of = `prompt:${p.id}`
  const here = { ...list, edit: p.id }
  if (history) {
    return <TextHistory crumbs={[...crumbs, { label: p.name, href: format(here) }]} source={textHistory(of)}
                        current={p.text} onRestore={t => promptsApi.put(p.id, t).then(() => nav.back(here))} />
  }
  const more: MenuItem[] = p.edited
    ? [{ label: 'Use the default', icon: RotateCcw, onSelect: () => void promptsApi.put(p.id, null).then(v => { res.set(v); setRound(n => n + 1) }) }]
    : []
  return (
    <LongEditor key={round} crumbs={crumbs} title={p.name} help={p.about || undefined} of={of} history={format({ ...here, history: true })}
                more={more} hint="Make it shorter, in plain words"
                texts={[{ key: 'text', label: p.name, value: p.text }]}
                autosave onSave={async v => { if ((v.text ?? '') !== p.text) res.set(await promptsApi.put(p.id, v.text ?? '')) }} />
  )
}
