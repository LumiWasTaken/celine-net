/* Celine chat: its place's list as the server groups it, its search, its mood
   next to its name, and Branch from here. */
import { json, request, type ConversationSummary } from './api'
import { useServerEvents } from './bus'
import { useResource } from './useResource'

/** its mood next to its name: 1 to 24 emojis or a kaomoji, its own to set (GET /mood, event
    `mood`); null until it set one */
export type Mood = { text: string; set_at: string }

/** POST /conversations/{cid}/branch {message_id}: a new conversation with every message up to
    and including this one; the server names it (event conversation.created) */
type ChatBranch = { id: string; title: string }

/** a shelf of its chats: the rows in the server's order (each with its `group`, `state`
    and `source`) and how many the shelf holds */
type ChatShelf = { rows: ConversationSummary[]; count: number }

/** found in a message: its conversation, the text around the match and the matched ranges
    in it, [start, end) */
export type ChatHit = {
  conversation_id: string
  message_id: string
  title: string
  snippet: string
  marks: [number, number][]
  /** the path that opens its conversation where it lives, the server's (homes.py) */
  link: string
}

const shelfQuery = (shelf: 'list' | 'archive' | 'trash') =>
  shelf === 'archive' ? '&archived=true' : shelf === 'trash' ? '&trashed=true' : ''

export const chatApi = {
  /** every row of its place on one shelf, page by page */
  list: async (shelf: 'list' | 'archive' | 'trash'): Promise<ChatShelf> => {
    const rows: ConversationSummary[] = []
    let cursor: string | null = null
    let count = 0
    do {
      const page: { conversations: ConversationSummary[]; next: string | null; count: number } = await request(
        `/conversations?place=celine&limit=500${shelfQuery(shelf)}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`)
      rows.push(...page.conversations)
      count = page.count
      cursor = page.next
    } while (cursor)
    return { rows, count }
  },
  /** the titles on that shelf that match (ids, best first), then the messages that match */
  search: (q: string, shelf: 'list' | 'archive' | 'trash', signal?: AbortSignal) =>
    request<{ titles: string[]; hits: ChatHit[] }>(
      `/messages?place=celine&q=${encodeURIComponent(q)}${shelfQuery(shelf)}`, { signal }),
  branch: (id: string, message_id: string) =>
    request<ChatBranch>(`/conversations/${encodeURIComponent(id)}/branch`, json('POST', { message_id })),
}

export const moodApi = {
  get: () => request<{ mood: Mood | null }>('/agent/mood'),
}

/** its mood as it set it last: fetched once, then each `mood` event */
export function useMood(): Mood | null {
  const res = useResource(() => moodApi.get().then(r => r.mood), [], { onReconnect: true })
  useServerEvents(ev => { if (ev.type === 'mood') res.set((ev.mood as Mood | null) ?? null) })
  return res.data ?? null
}
