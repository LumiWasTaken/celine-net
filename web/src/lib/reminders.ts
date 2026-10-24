/* Scheduled messages (a reminder is an intercom message with a time): the api client
   (GET /intercom?to=&scheduled=true; setting one goes through intercomApi.send, Cancel
   through intercomApi.cancel), the row type and the context that hands the open conversation's list, with each one's state,
   to the tool rows. */
import { createContext } from 'react'
import { request } from './api'
import { intercomApi } from './intercom'

/** one message scheduled to a conversation; `from` 'user' is yours, `agent:<cid>` its own */
export type Reminder = {
  id: number
  from: string
  to: string
  due_at: string
  text: string
  state: 'pending' | 'sent' | 'cancelled'
}

/** the When field as the server takes it: `08:00` is `at`, anything else is `in` */
const when = (s: string) => (/^\d{1,2}:\d\d$/.test(s) ? { at: s } : { in: s })

export const remindersApi = {
  /** every message scheduled to one conversation, with its state */
  list: (conversation: string) =>
    request<{ messages: Reminder[] }>(`/intercom?to=${encodeURIComponent(`agent:${conversation}`)}&scheduled=true`).then(r => r.messages),
  set: (conversation: string, whenText: string, text: string) =>
    intercomApi.send({ to: `agent:${conversation}`, ...when(whenText.trim()), text }),
}

/** the open conversation's scheduled messages with their state (null until fetched, and
 *  outside Celine), for the rows in its transcript; `reload` after a Cancel so nothing
 *  waits for the event */
export const ConversationReminders = createContext<{ list: Reminder[] | null; reload: () => void }>({ list: null, reload: () => {} })
