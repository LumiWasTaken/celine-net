/* The features view's pure rules, shared by Settings, Features and the onboarding (checked
   by featureSteps.check.mjs in the prebuild). */
import type { Feature, FeaturesView } from './settingsApi'

/** a feature's switch cannot be on: the server says so (its probe's `blocks`) */
export const blocked = (f: Feature) => f.blocks

/** a feature `after` another whose switch, as flipped, is off: off and greyed */
export const waits = (view: FeaturesView, f: Feature) => !!f.after && view.features[f.after] === false

/** the switches with `id` flipped to `on`: off, every feature after it goes off too */
export const flip = (view: FeaturesView, id: string, on: boolean) => ({
  ...view.features, [id]: on,
  ...(on ? {} : Object.fromEntries(view.catalog.filter(f => f.after === id).map(f => [f.id, false]))),
})

/** the onboarding's steps before the features' own */
const FIXED = ['her', 'you', 'model', 'features']

/** a feature switched on that has a setup step: its own step, or its group's */
export const stepping = (view: FeaturesView) => (f: Feature) => f.step && view.features[f.id] !== false

/** a feature's step id: its group's, or its own */
const stepOf = (f: Feature) => (f.group_label ? f.group : f.id)

/** the onboarding's step ids in order: the fixed four, one per feature switched on with a
    setup step in the catalog's order (its feature id, or its group's id once), done */
export const stepsOf = (view: FeaturesView | null) => {
  const on = view ? view.catalog.filter(stepping(view)) : []
  return [...FIXED, ...new Set(on.map(stepOf)), 'done']
}

/** where a step id lands: itself while it is a step; a feature of a group, its group's
    step; a step that is not on now, the first step on after it in the full order (every
    feature with a setup step), else Features */
export const landing = (view: FeaturesView, step: string) => {
  const steps = stepsOf(view)
  const f = view.catalog.find(x => x.id === step)
  const id = f ? stepOf(f) : step
  if (steps.includes(id)) return id
  const all = [...FIXED, ...new Set(view.catalog.filter(x => x.step).map(stepOf)), 'done']
  const at = all.indexOf(id)
  return (at < 0 ? undefined : all.slice(at + 1).find(s => steps.includes(s) && s !== 'done')) ?? 'features'
}

/** the switches the onboarding starts from: before anything was saved, what is on and not
    blocked, and a service that answers (the server keeps what needs a service off until
    Setup saves it), never one that asks first (`warn`); once saved, what is saved */
export const firstTime = (v: FeaturesView): FeaturesView => v.saved ? v : ({
  ...v,
  features: Object.fromEntries(v.catalog.map(f => [f.id,
    !blocked(f) && (v.features[f.id] !== false || (f.state === 'ready' && !f.warn))])),
})

/** what a PUT saves: each switch as flipped, but a blocked one off unless `stored` (the
    server's last answer) already has it on: a service down for a moment keeps its switch */
export const toSave = (view: FeaturesView, stored: Record<string, boolean>) =>
  Object.fromEntries(view.catalog.map(f => [f.id,
    view.features[f.id] !== false && (!blocked(f) || stored[f.id] === true)]))
