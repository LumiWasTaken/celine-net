/* It, from GET /api/status `owner`: its name, pronouns, colour and portrait, the one
   person every page draws (lib/agent.ts). Settings, its page (views/settings/Agent.tsx) edits it through
   GET/PUT /api/agent. */
import { createContext, useContext } from 'react'

export type Agent = {
  name: string
  pronouns: 'she' | 'he' | 'they' | 'it'
  /** an accent from the palette (COLORS) */
  color: string
  /** its name was saved once (first run's "Set its name") */
  named: boolean
  portrait: { url: string } | null
}

/** null while the status has not told of it */
export const AgentContext = createContext<Agent | null>(null)
export const useAgent = () => useContext(AgentContext)

/** the palette's accents it can take, in the order Settings offers them */
export const COLORS = ['rose', 'peach', 'sage', 'teal', 'sky', 'violet', 'miku'] as const

export const colorOf = (c?: Pick<Agent, 'color'> | null) =>
  `var(--char-${COLORS.includes((c?.color ?? '') as typeof COLORS[number]) ? c!.color : 'rose'})`
