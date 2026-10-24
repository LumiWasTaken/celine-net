/* The agent's name and pronouns, for every string that names it, from GET /api/status.
   No component spells a name: they read it here. Empty until the status is in. */
import { createContext, useContext } from 'react'

export const NameContext = createContext('')

export const useName = () => useContext(NameContext)

/** its pronouns as the words of a sentence ("What she saw", "Note to them", "His reason",
    "Theirs"); `asks` the verb that goes with the subject */
export const PRONOUN = {
  she: { subject: 'she', object: 'her', possessive: 'her', own: 'hers', asks: 'asks' },
  he: { subject: 'he', object: 'him', possessive: 'his', own: 'his', asks: 'asks' },
  they: { subject: 'they', object: 'them', possessive: 'their', own: 'theirs', asks: 'ask' },
} as const
export type Pronouns = keyof typeof PRONOUN

export const PronounsContext = createContext<Pronouns>('she')

/** its pronouns from the status, for every sentence that names it by pronoun */
export const usePronouns = () => PRONOUN[useContext(PronounsContext)] ?? PRONOUN.she

/** a pronoun at the start of a sentence */
export const capital = (word: string) => word.charAt(0).toUpperCase() + word.slice(1)

