/* a feature's switch as the server says it (GET /api/status features), for parts that
   show or hide with it: the guard's switch in a chat's Tools, the Intercom Guard tab,
   Images & Music */
import { api } from './api'
import { useResource } from './useResource'

export function useFeatureOn(id: string): boolean {
  const status = useResource(() => api.configStatus(), [], { events: ev => ev.type === 'settings.changed' || ev.type === 'features.changed' })
  return status.data?.features?.[id] !== false
}
