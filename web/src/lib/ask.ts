/* "Ask Celine" from anywhere (a search, a message): a new conversation opens with a
   draft. The words go into the box; messages go along as references shown above the
   box, never as [[…]] text you would have to read or type around. Nothing is sent. */
import { emit } from './bus'
import { copy } from '../ui'
import type { Route } from './nav'

export function askCeline(nav: { go: (r: Route) => void }, text: string, refs: string[] = []) {
  emit({ type: 'celine.prefill', text, refs })
  nav.go({ section: 'celine' })
}

/** From beside the open conversation (the side panel): the text and references go into
    its box after what is there. No navigation, no focus change, nothing sent. */
export function draftToCeline(text: string, refs: string[] = []) {
  emit({ type: 'celine.draft', text, refs })
}

/** a message's link ([[whatsapp:123]]) on the clipboard; false when the browser refuses */
export async function copyMessageLink(ref: string): Promise<boolean> {
  return copy(`[[${ref}]]`)
}
