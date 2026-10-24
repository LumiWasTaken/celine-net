/* One skill as a block, the same in Settings, Tools and a conversation's Tools tab: its
   switch (every tool of the skill at once; mixed when they differ), its name, a muted word
   when it is down, stopped or not set up (its error on hover; a stopped one has Restart),
   and the first sentence of what it does. "N tools" folds open to one switch per tool,
   each with the pen when it changes data and, where the place has a default to go back
   to, Use the default. Folded by default; this device keeps the click per place and skill.
   The switches show what the caller says is on: the server's answer. */
import { PenLine, RotateCcw } from 'lucide-react'
import { OFFLINE_TITLE } from '../lib/socket'
import { SKILL_WORD, type SkillView } from '../lib/settingsApi'
import { Fold, ICON, IconButton, Switch } from '../ui'
import './ToolBlock.css'

/** `here`: this place sets it apart from its default */
export type BlockTool = { wire: string; label: string; summary: string; mutating: boolean; on: boolean; here: boolean }
export type BlockSkill = {
  name: string; label: string; summary: string; tools: BlockTool[]
  status: SkillView['status']; error: string | null
}

/** the first sentence of a description: one line */
const line = (s: string) => s.split(/(?<=\.)\s/)[0] ?? ''

/** a tool under its skill without the skill's name it starts with ("Notebook: delete a
    note" is "Delete a note") */
const shortLabel = (label: string) => {
  const t = label.replace(/^[^:]+:\s+/, '')
  return t.charAt(0).toUpperCase() + t.slice(1)
}

export function ToolBlock({ skill: s, place, writable, open, onSet, onReset, onRestart, restarting }: {
  skill: BlockSkill
  /** where it is drawn: the fold's click is kept per place */
  place: string
  /** false offline: every switch is greyed and says why */
  writable: boolean
  /** the tools shown without their fold (a search) */
  open?: boolean
  /** a skill's name or a tool's wire name */
  onSet: (target: string, on: boolean) => void
  onReset?: (tool: BlockTool) => void
  onRestart?: () => void
  restarting?: boolean
}) {
  const lock = writable ? undefined : OFFLINE_TITLE
  const on = s.tools.filter(t => t.on).length
  const usable = !SKILL_WORD[s.status]
  const state = !usable || on === 0 ? false : on === s.tools.length ? true : 'mixed' as const
  const word = SKILL_WORD[s.status] ?? (state === 'mixed' ? `${on} of ${s.tools.length} on` : null)
  const rows = (
    <div className="tool-rows">
      {s.tools.map(t => (
        <div key={t.wire} className="tool-row">
          <Switch className="tool-row__switch" checked={t.on} disabled={!writable} title={lock}
                  onChange={next => onSet(t.wire, next)}
                  label={<><span className="tool-row__name">{shortLabel(t.label)}</span>
                           {t.summary && <span className="tool-row__line">{line(t.summary)}</span>}</>} />
          {t.mutating && <span className="tool-row__writes" role="img" aria-label="Changes data" title="Changes data"><PenLine size={ICON.xs} aria-hidden /></span>}
          {/* set on the tool itself, it wins over its skill's switch */}
          {t.here && onReset && (
            <IconButton icon={RotateCcw} size="sm" label={`${t.label}: use the default`}
                        disabled={!writable} title={lock} onClick={() => onReset(t)} />
          )}
        </div>
      ))}
    </div>
  )
  return (
    <div className="tool-block">
      <div className="tool-block__head">
        <Switch className="tool-block__switch" checked={state} disabled={!usable || !writable || s.tools.length === 0}
                title={lock} onChange={next => onSet(s.name, next)}
                label={<span className="tool-block__name">{s.label}</span>} />
        {word && <span className="tool-block__state" title={s.error ?? undefined}>{word}</span>}
        {s.status === 'dead' && onRestart && (
          <IconButton icon={RotateCcw} size="sm" label={`Restart ${s.label}`} loading={restarting} onClick={onRestart} />
        )}
      </div>
      {s.summary && <p className="tool-block__line">{line(s.summary)}</p>}
      {usable && s.tools.length > 0 && (open ? rows : (
        <Fold title={`${s.tools.length} tools`} className="tool-block__fold" remember={`${place}:${s.name}`}>{rows}</Fold>
      ))}
    </div>
  )
}
