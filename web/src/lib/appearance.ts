/* Settings, Appearance: the server's choices, written
   as attributes and variables on <html> (tokens.css reads them) and given to every view
   through AppearanceContext, which App.tsx provides. Loaded again on `settings.changed`.
   The server's look is the one that counts; this device keeps a copy of the last one it
   applied (with the canvas colour it made) so public/theme.js paints it before the app
   loads, and the app starts from it until the server answers. */
import { createContext, useContext, useEffect, useMemo, useState, useSyncExternalStore } from 'react'
import { json, request } from './api'
import { getJson, setJson } from './prefs'
import { useResource } from './useResource'

/** GET/PUT /appearance, event settings.changed */
export const ACCENTS = ['rose', 'peach', 'sage', 'teal', 'sky', 'violet', 'miku'] as const
export type Accent = typeof ACCENTS[number]
export const GRAINS = ['fine', 'coarse', 'paper', 'none'] as const
export type Grain = typeof GRAINS[number]
export type Appearance = {
  accent: Accent
  grain: Grain
  /** percents, as settings/registry.py APPEARANCE: the grain's opacity (0 to 20), the
      accent's tint of the greys (0 to 300), the lightness step between surfaces (50 to 150) */
  grain_opacity: number
  tint: number
  contrast: number
  text_size: 'sm' | 'md' | 'lg' | 'xl'
  hide_thinking: boolean
  hide_tools: boolean
  collapse_thinking: boolean
}

/** Settings, Appearance; a key sent as null goes back to the server's default */
export const appearanceApi = {
  get: () => request<{ appearance: Appearance }>('/appearance'),
  put: (appearance: { [K in keyof Appearance]?: Appearance[K] | null }) =>
    request<{ appearance: Appearance }>('/appearance', json('PUT', { appearance })),
}

/** the look until the server answers, on a device that kept none */
export const DEFAULT_APPEARANCE: Appearance = {
  accent: 'rose',
  grain: 'fine',
  grain_opacity: 4,
  tint: 100,
  contrast: 100,
  text_size: 'md',
  hide_thinking: false,
  hide_tools: false,
  collapse_thinking: true,
}

export const AppearanceContext = createContext<Appearance>(DEFAULT_APPEARANCE)
export const useAppearance = () => useContext(AppearanceContext)

/* Settings, Appearance: a change shows on the whole app at once, while its save is on its
   way (Appearance.tsx saves it); a key the server holds needs no preview */
let preview: Partial<Appearance> = {}
const watchers = new Set<() => void>()
export function previewAppearance(change: Partial<Appearance>) {
  preview = change
  watchers.forEach(f => f())
}
const watch = (f: () => void) => { watchers.add(f); return () => { watchers.delete(f) } }
/* App.tsx's copy of the server's look, which a save's answer replaces */
let settle: ((a: Appearance) => void) | null = null

/** PUT the keys (null: back to the server's default); its answer is the app's look at once */
export async function saveAppearance(change: Parameters<typeof appearanceApi.put>[0]) {
  const { appearance: a } = await appearanceApi.put(change)
  settle?.(a)
  previewAppearance(Object.fromEntries(Object.entries(preview).filter(([k, v]) => a[k as keyof Appearance] !== v)))
}

/* this device's copy (public/theme.js reads the same key) */
const COPY = 'look'
type Copy = Appearance & { canvas: string }

function apply(a: Appearance) {
  const h = document.documentElement
  h.dataset.accent = a.accent
  h.dataset.textSize = a.text_size
  h.dataset.grain = a.grain
  h.style.setProperty('--grain-opacity', String(a.grain_opacity / 100))
  h.style.setProperty('--tint', String(a.tint / 100))
  h.style.setProperty('--contrast', String(a.contrast / 100))
}

/** App.tsx: the server's appearance with the change on its way, applied to <html> */
export function useAppearanceSettings(): Appearance {
  const r = useResource(() => appearanceApi.get().then(x => x.appearance), [],
                        { events: ev => ev.type === 'settings.changed' })
  const [kept] = useState(() => ({ ...DEFAULT_APPEARANCE, ...getJson<Partial<Copy>>(COPY, {}) }))
  useEffect(() => { settle = r.set; return () => { settle = null } }, [r.set])
  const previewing = useSyncExternalStore(watch, () => preview)
  const a = r.data ?? kept
  const shown = useMemo(() => ({ ...a, ...previewing }), [a, previewing])
  useEffect(() => {
    apply(shown)
    // the device's copy follows each save
    if (Object.keys(previewing).length || !r.data) return
    const canvas = getComputedStyle(document.body).backgroundColor
    setJson(COPY, { ...r.data, canvas } satisfies Copy)
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', canvas)
  }, [shown, previewing, r.data])
  return shown
}
