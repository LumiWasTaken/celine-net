/* One conversation's settings (GET/PATCH /conversations/{cid}/settings) as every part of its
   panel reads them: loaded while `enabled`, reloaded on settings events, replaced by what
   the conversation's socket pushes (`live`: /think, another tab), patched in place. */
import { useEffect } from 'react'
import { SETTINGS_EVENTS, settingsApi, type ConversationPatch, type ConversationSettings } from './settingsApi'
import { useAction } from './useAction'
import { useResource } from './useResource'

export function useConversationSettings(cid: string, enabled: boolean, live: ConversationSettings | null) {
  const res = useResource(() => settingsApi.conversationSettings(cid), [cid], { enabled, events: SETTINGS_EVENTS })
  const { set } = res
  useEffect(() => { if (live) set(live) }, [live, set])
  const patch = useAction(async (p: ConversationPatch) => { set(await settingsApi.patchConversationSettings(cid, p)) })
  return { res, patch }
}
