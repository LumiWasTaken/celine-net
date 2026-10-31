/* What the model servers have loaded now (GET /api/loaded-models, then each
   `model.loaded` event): every model of a provider whose server says what it has
   loaded, with `loaded` and the seconds its last load took, measured by the server. */
import { request } from './api'
import { useServerEvents } from './bus'
import { SETTINGS_EVENTS } from './settingsApi'
import { useResource } from './useResource'

export type Loaded = {
  models: { ref: string; loaded: boolean; load_seconds: number | null }[]
}

export function useLoaded(): Loaded | undefined {
  const res = useResource(() => request<Loaded>('/loaded-models'), [], { events: SETTINGS_EVENTS })
  useServerEvents(ev => {
    if (ev.type === 'model.loaded' && Array.isArray(ev.models)) {
      res.set({ models: ev.models as Loaded['models'] })
    }
  })
  return res.data
}
