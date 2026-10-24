/* It, from GET /api/status `owner`: its name, pronouns, colour and portrait, the one
   person every page draws (lib/her.ts). Settings, its page (views/settings/Her.tsx) edits it through
   GET/PUT /api/agent. */
import { createContext, useContext } from 'react'

export type Her = {
  name: string
  pronouns: 'she' | 'he' | 'they'
  /** an accent from the palette (COLORS) */
  color: string
  /** its name was saved once (first run's "Set its name") */
  named: boolean
  portrait: { url: string } | null
}

/** null while the status has not told of it */
export const HerContext = createContext<Her | null>(null)
export const useHer = () => useContext(HerContext)

/** the palette's accents it can take, in the order Settings offers them */
export const COLORS = ['rose', 'peach', 'sage', 'teal', 'sky', 'violet', 'miku'] as const

export const colorOf = (c?: Pick<Her, 'color'> | null) =>
  `var(--char-${COLORS.includes((c?.color ?? '') as typeof COLORS[number]) ? c!.color : 'rose'})`
