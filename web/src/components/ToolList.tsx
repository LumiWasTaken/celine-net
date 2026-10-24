/* The agent's tools in a conversation's side panel, grouped as Settings, Tools groups them
   (TOOL_GROUPS, toolOrder), one ToolBlock per skill, switched for this conversation only
   (or for a new one, kept as a draft until its first message). Search finds a tool by its
   name, what it does or the name Celine calls it by, and shows the tools it found unfolded. */
import { useMemo, useState } from 'react'
import { conversationApi, type SkillInfo } from '../lib/api'
import { errorText, nothingMatches } from '../lib/format'
import { useOnline } from '../lib/socket'
import { TOOL_GROUPS, settingsApi, toolGroup, toolOrder, type SkillView } from '../lib/settingsApi'
import { useResource } from '../lib/useResource'
import { EmptyState, InlineError, ListSection, LoadingRow, SearchField } from '../ui'
import { ToolBlock, type BlockSkill as Skill, type BlockTool as Tool } from './ToolBlock'
import './ToolList.css'

/* names spelled their own way wherever a label is made from an id */
const PROPER: Record<string, string> = { whatsapp: 'WhatsApp', discord: 'Discord', github: 'GitHub', searxng: 'SearXNG' }
const humanize = (s: string) => {
  const t = s.replace(/_/g, ' ').split(' ').map(w => w.replace(/^(\w+)/, x => PROPER[x.toLowerCase()] ?? x)).join(' ')
  return t.charAt(0).toUpperCase() + t.slice(1)
}

/** labels for people, from the server's tool list (GET /api/tool-defaults) */
export function useToolLabels(): (wire: string) => string {
  const res = useResource(() => settingsApi.tools().then(r => r.skills).catch(() => [] as SkillView[]), [])
  return useMemo(() => {
    const m = new Map<string, string>()
    for (const s of res.data ?? []) for (const t of s.tools) m.set(t.wire_name, t.label)
    return (wire: string) => m.get(wire) ?? humanize(wire.replace('__', ': '))
  }, [res.data])
}

function fromView(s: SkillView): Skill {
  return {
    name: s.name, label: s.label, summary: s.summary, status: s.status, error: s.error,
    tools: s.tools.map(t => ({ wire: t.wire_name, label: t.label, summary: t.summary, mutating: t.mutating, on: t.default === 'on', here: false })),
  }
}

type Loaded = { skills: Skill[] }

/* the Settings defaults: a draft's base */
async function loadDefaults(): Promise<Loaded> {
  return { skills: (await settingsApi.tools()).skills.map(fromView) }
}

/* A conversation: its own switches (GET /api/skills?conversation=). */
function fromConversation(s: SkillInfo): Skill {
  return {
    name: s.name, label: s.label, summary: s.summary, status: s.status, error: s.error,
    tools: s.tools.map(t => ({ wire: t.wire_name, label: t.label, summary: t.summary, mutating: t.mutating,
                               on: t.enabled, here: t.enabled !== t.default })),
  }
}

async function loadConversation(cid: string): Promise<Loaded> {
  return { skills: (await conversationApi.skills(cid)).skills.map(fromConversation) }
}

/** choices for a conversation that does not exist yet: target (skill or tool) → on/off */
type ToolDraft = { value: Record<string, boolean>; onChange: (next: Record<string, boolean>) => void }

/** the draft applied on top of the defaults: a tool's own entry wins over its skill's */
function withDraft(skills: Skill[], d: Record<string, boolean>): Skill[] {
  return skills.map(s => ({
    ...s,
    tools: s.tools.map(t => {
      const own = d[t.wire], bySkill = d[s.name]
      const on = own ?? bySkill ?? t.on
      return { ...t, on, here: on !== t.on }
    }),
  }))
}

export function ToolList({ conversationId, draft }: {
  /** a conversation: its own switches */
  conversationId?: string | null
  /** a new conversation: switches kept here until the first message creates it */
  draft?: ToolDraft
}) {
  const res = useResource(() => (conversationId ? loadConversation(conversationId) : loadDefaults()),
                          [conversationId],
                          { events: ev => ev.type === 'settings.changed' })
  const online = useOnline()
  const [q, setQ] = useState('')
  const [error, setError] = useState<string | null>(null)
  const skills = useMemo(() => {
    const base = toolOrder(res.data?.skills ?? [])
    return draft ? withDraft(base, draft.value) : base
  }, [res.data, draft])

  const groups = useMemo(() => {
    const f = q.trim().toLowerCase()
    const hit = (t: Tool) => !f || t.label.toLowerCase().includes(f) || t.wire.toLowerCase().includes(f) || t.summary.toLowerCase().includes(f)
    return TOOL_GROUPS.map(([g, label]) => ({
      g, label,
      skills: skills.filter(s => toolGroup(s.status, s.tools.some(t => t.on)) === g).flatMap(s => {
        if (!f || s.label.toLowerCase().includes(f) || s.name.toLowerCase().includes(f)) return [s]
        const tools = s.tools.filter(hit)
        return tools.length ? [{ ...s, tools }] : []
      }),
    })).filter(x => x.skills.length > 0)
  }, [skills, q])

  const set = async (target: string, on: boolean) => {
    setError(null)
    const skill = target.includes('__') ? undefined : target
    if (draft) {
      // a skill's switch is the bulk verb: it clears the tool choices under it
      const next = Object.fromEntries(Object.entries(draft.value).filter(([k]) => !(skill && k.startsWith(`${skill}__`))))
      next[target] = on
      draft.onChange(next)
      return
    }
    // show it at once; the reload brings the server's answer
    res.set(d => d && {
      ...d,
      skills: d.skills.map(s => ({
        ...s,
        tools: s.tools.map(t => (t.wire === target || s.name === skill ? { ...t, on } : t)),
      })),
    })
    // the server keeps the newest client_seq per target: a clock grows across mounts and reloads
    try { if (conversationId) await conversationApi.setTool(conversationId, target, on, Date.now()) }
    catch (e) { setError(errorText(e)) }
    res.reload()
  }
  /* "use the default": drop the tool's own switch. When its skill's switch here still
     sets it apart, pin it to the default instead. */
  const reset = async (s: Skill, t: Tool) => {
    setError(null)
    const def = !t.on
    if (draft) {
      const { [t.wire]: _, ...next } = draft.value
      const bySkill = next[s.name]
      draft.onChange(bySkill === undefined || bySkill === def ? next : { ...next, [t.wire]: def })
      return
    }
    if (!conversationId) return
    try {
      await settingsApi.patchConversationSettings(conversationId, { tools: { [t.wire]: null } })
      const after = (await loadConversation(conversationId)).skills.flatMap(x => x.tools).find(x => x.wire === t.wire)
      if (after?.here) await conversationApi.setTool(conversationId, t.wire, def, Date.now())
    } catch (e) { setError(errorText(e)) }
    res.reload()
  }
  const restart = async (name: string) => {
    setError(null)
    try { await conversationApi.retrySkill(name) } catch (e) { setError(errorText(e)) }
    res.reload()
  }

  if (res.error && res.data === undefined) return <InlineError message={res.error} onRetry={res.reload} />
  if (res.data === undefined) return <LoadingRow label="Loading tools" />
  const writable = online || !!draft
  return (
    <div className="tools">
      <SearchField value={q} onChange={setQ} placeholder="Search tools" />
      {error && <InlineError compact message={error} />}
      {groups.length === 0 ? <EmptyState compact>{nothingMatches(q)}</EmptyState> : groups.map(({ g, label, skills }) => (
        <div key={g} className="tool-blocks">
          <ListSection label={label} />
          {skills.map(s => (
            <ToolBlock key={s.name} skill={s} place="chat-tools" writable={writable} open={!!q.trim()}
                       onSet={(target, on) => void set(target, on)} onReset={t => void reset(s, t)}
                       onRestart={() => void restart(s.name)} />
          ))}
        </div>
      ))}
    </div>
  )
}
