/* Tools: the default for every conversation; each conversation can switch tools in its
   side panel. Where: all of them, or the skills one place offers (its chats, Missions,
   Routines; the server's places), picked in the Toolbar.
   Require approval first: the tools whose every call waits for your answer, each by its
   skill and its name with Remove; Add picks one from every tool (the menu's search finds
   it by either name). Then three groups: On, Not set up (shown off until it works), Off,
   each A to Z (toolOrder, the panel's order too). One block per skill (ToolBlock, as in
   the panel). */
import { useState } from 'react'
import { MapPin, Trash2 } from 'lucide-react'
import { conversationApi } from '../../lib/api'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { TOOL_GROUPS, settingsApi, toolGroup, toolOrder, type SkillView } from '../../lib/settingsApi'
import { useAction } from '../../lib/useAction'
import { useAutosave } from '../../lib/useAutosave'
import { useResource } from '../../lib/useResource'
import { ToolBlock, shortLabel } from '../../components/ToolBlock'
import { EmptyState, IconButton, InlineError, ListRow, Loaded, MenuButton, Section, Toolbar } from '../../ui'
import { Page } from './parts'
import './settings.css'

/* what GET /tool-defaults adds for the places: each skill's, and their words */
type Placed = SkillView & { places: string[] }
type Places = { name: string; label: string }[]

const load = () => settingsApi.tools().then(r => {
  const placed = r as typeof r & { skills: Placed[]; places?: Places }
  return { skills: toolOrder(placed.skills), places: placed.places ?? [] }
})

export function Tools() {
  const online = useOnline()
  const [where, setWhere] = useState('all')
  const res = useResource(load, [], { events: ev => ev.type === 'settings.changed' })
  // a switch saves at once (a skill's or one tool's); the list after it comes in place
  const set = useAutosave<boolean>(async changes => {
    for (const [target, on] of Object.entries(changes)) await settingsApi.putTool(target, on ? 'on' : 'off')
    res.set(await load())
  })
  const restart = useAction(async (name: string) => { await conversationApi.retrySkill(name); res.reload() })
  const skills = res.data?.skills.filter(s => where === 'all' || s.places.includes(where))
  const places = res.data?.places ?? []
  return (
    <Page title="Tools">
      {places.length > 0 && (
        <Toolbar filters={[{ label: 'Where', icon: MapPin, picked: places.find(p => p.name === where)?.label, onClear: () => setWhere('all'),
                             items: [{ label: 'All', current: where === 'all', onSelect: () => setWhere('all') },
                                     ...places.map(p => ({ label: p.label, current: where === p.name, onSelect: () => setWhere(p.name) }))] }]} />
      )}
      {res.data && <Approval skills={res.data.skills} writable={online} onSaved={async () => res.set(await load())} />}
      {!skills ? (
        <Section>{<Loaded res={res} label="Loading tools" />}</Section>
      ) : TOOL_GROUPS.map(([g, title]) => {
        const shown = skills.filter(s => toolGroup(s.word, s.default !== 'off') === g)
        if (!shown.length) return null
        return (
          <Section key={g} title={title}>
            {g === 'on' && set.error && <InlineError compact message={set.error} onRetry={set.flush} />}
            {g === 'on' && restart.error && <InlineError compact message={restart.error} />}
            <div className="tool-blocks">
              {shown.map(s => (
                <ToolBlock key={s.name} place="tools" writable={online} onSet={set.set}
                           onRestart={() => void restart.run(s.name)} restarting={restart.busy}
                           skill={{ ...s, tools: s.tools.map(t => ({
                             wire: t.wire_name, label: t.label, summary: t.summary, mutating: t.mutating, here: false,
                             on: set.pending[t.wire_name] ?? set.pending[s.name] ?? t.default === 'on' })) }} />
              ))}
            </div>
          </Section>
        )
      })}
    </Page>
  )
}

/* ── Require approval: the tools on it, Remove on each; Add from every other tool ── */
function Approval({ skills, writable, onSaved }: { skills: SkillView[]; writable: boolean; onSaved: () => Promise<void> }) {
  const save = useAction(async (tool: string, on: boolean) => { await settingsApi.putApproval(tool, on); await onSaved() })
  const tools = skills.flatMap(s => s.tools.map(t => ({ wire: t.wire_name, name: `${s.label}: ${shortLabel(t.label)}`, on: t.approval })))
    .sort((a, b) => a.name.localeCompare(b.name))
  const lock = writable ? undefined : OFFLINE_TITLE
  return (
    <Section title="Require approval"
             action={<MenuButton text="Add" label="Add" size="sm" search="Search tools"
                                 items={tools.filter(t => !t.on).map(t => ({ label: t.name, disabled: !writable, title: lock,
                                                                            onSelect: () => void save.run(t.wire, true) }))} />}>
      {save.error && <InlineError compact message={save.error} />}
      {!tools.some(t => t.on) && <EmptyState compact>No tools require approval.</EmptyState>}
      <div className="rows">
        {tools.filter(t => t.on).map(t => (
          <ListRow key={t.wire} title={t.name} trailingAlways
                   trailing={<IconButton icon={Trash2} label="Remove" disabled={!writable || save.busy} title={lock}
                                         onClick={() => void save.run(t.wire, false)} />} />
        ))}
      </div>
    </Section>
  )
}
