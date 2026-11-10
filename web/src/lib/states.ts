/* A state as the server says it (server/celine/core/states.py): its words and its tone.
   Missions, tree nodes, sub-agents and runs carry theirs on each row (`words`, `tone`);
   a list also carries the whole table (`states`) for its filter and its group heads. */
import type { WordTone } from '../ui'

export type Said = { words: string; tone: 'running' | 'ok' | 'error' | 'muted' | 'warn' }

/** the kit's tone of a server tone */
export const toneOf = (t: Said['tone']): WordTone => (t === 'ok' ? 'success' : t)
