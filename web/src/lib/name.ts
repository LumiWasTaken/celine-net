/* The agent's name, for every string that names it, from GET /api/status. No component
   spells a name or a pronoun for it: they read the name here. Empty until the status is in. */
import { createContext, useContext } from 'react'

export const NameContext = createContext('')

export const useName = () => useContext(NameContext)

/** whose a thing is, for a label: its name with 's ("Celine's thoughts"), "Your agent's"
    while the name is not in yet */
export function useOwner() {
  const name = useName()
  return name ? `${name}'s` : "Your agent's"
}

/** a word at the start of a sentence */
export const capital = (word: string) => word.charAt(0).toUpperCase() + word.slice(1)
