/* Forms drawn from the server's field schema, one set of routes for every form
   (server/celine/api/forms.py): GET /forms/{id}, PUT /forms/{id} with If-Match,
   POST /forms/{id}/{action}. A secret is write-only: a string sets it, null removes it,
   left out it stays; the view only says {state, set_at}.

     const form = useForm(id)
     const auto = useAutosave(changes => form.save(changes))       // a Settings page
     <Form {...form.shown(auto.pending)} fields={form.res.data.fields} onChange={auto.set} />

   An onboarding card keeps its changes as a draft and calls `save(draft)` from its
   saveRef. An action never saves: save first when the draft changed. Actions are offered
   only while the view says `running`. */
import { json, request } from './api'
import { useResource, type Resource } from './useResource'
import type { DotTone, FormField, SecretState } from '../ui'

export type FormAction = { id: string; label: string; /** the label of the one text it asks for */ input?: string }
export type FormView = {
  fields: FormField[]
  actions: FormAction[]
  note?: string
  values: Record<string, unknown>
  secrets: Record<string, SecretState>
  version: string
  saved: boolean
  /** its actions can run now (a plugin's code runs only while it is on) */
  running: boolean
}
/** `lines`: what it found, one Dot each (the Mac's Check: each server, each permission) */
export type ActionResult = { ok: boolean; sentence: string; lines?: { text: string; tone: DotTone }[] }

const enc = encodeURIComponent

export const formsApi = {
  get: (id: string) => request<FormView>(`/forms/${enc(id)}`),
  /** `values` whole (a key left out goes back to its default); `secrets` only the changed ones */
  put: (id: string, version: string, values: Record<string, unknown>, secrets: Record<string, string | null>) =>
    request<FormView>(`/forms/${enc(id)}`, {
      ...json('PUT', Object.keys(secrets).length ? { values, secrets } : { values }),
      headers: { 'If-Match': version },
    }),
  run: (id: string, action: string, input?: string) =>
    request<ActionResult>(`/forms/${enc(id)}/${enc(action)}`, json('POST', input === undefined ? {} : { input })),
}

export type UseForm = {
  res: Resource<FormView>
  /** PUT the view's values with `changes` on top; a secret's change goes as a secret */
  save: (changes: Record<string, unknown>) => Promise<void>
  /** what the Form shows with `changes` on top: a removed secret is not set, a typed one stays typed */
  shown: (changes: Record<string, unknown>) => { values: Record<string, unknown>; secrets: Record<string, SecretState> }
  run: (action: string, input?: string) => Promise<ActionResult>
}

export function useForm(id: string): UseForm {
  const res = useResource(() => formsApi.get(id), [id],
                          { events: ev => ev.type === 'settings.changed' || (ev.type === 'features.changed' && ev.id === id) })
  const isSecret = (k: string) => res.data?.fields.some(f => f.key === k && f.type === 'secret') ?? false
  return {
    res,
    save: async changes => {
      const d = res.data
      if (!d || !Object.keys(changes).length) return
      const values = { ...d.values }
      const secrets: Record<string, string | null> = {}
      for (const [k, v] of Object.entries(changes)) {
        if (isSecret(k)) secrets[k] = v as string | null
        else values[k] = v
      }
      res.set(await formsApi.put(id, d.version, values, secrets))
    },
    shown: changes => {
      const secrets = { ...res.data?.secrets }
      for (const [k, v] of Object.entries(changes)) if (v === null && isSecret(k)) secrets[k] = { state: 'not set', set_at: null }
      return { values: { ...res.data?.values, ...changes }, secrets }
    },
    run: (action, input) => formsApi.run(id, action, input),
  }
}
