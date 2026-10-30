/* The features view's pure rules, shared by Settings, Features and the onboarding (checked
   by featureSteps.check.mjs in the prebuild). */
import type { Feature, FeaturesView } from './settingsApi'

/** a feature's switch cannot be on: its service is missing or not set up */
export const blocked = (f: Feature) => f.state === 'missing' || f.state === 'unset'

/** the onboarding's steps before the features' own */
const FIXED = ['her', 'you', 'model', 'features']

/** a feature switched on that has a setup step: its own step, or its group's */
export const stepping = (view: FeaturesView) => (f: Feature) => f.step && view.features[f.id] !== false

/** the onboarding's step ids in order: the fixed four, one per feature switched on with a
    setup step in the catalog's order (its feature id, or its group's id once), done */
export const stepsOf = (view: FeaturesView | null) => {
  const on = view ? view.catalog.filter(stepping(view)) : []
  return [...FIXED, ...new Set(on.map(f => (f.group_label ? f.group : f.id))), 'done']
}

/** the switches the onboarding starts from: what is on and not blocked; before anything
    was saved also a service that answers (the server keeps what needs a service off until
    Setup saves it), never one that asks first (`warn`) */
export const firstTime = (v: FeaturesView): FeaturesView => ({
  ...v,
  features: Object.fromEntries(v.catalog.map(f => [f.id,
    !blocked(f) && (v.features[f.id] !== false || (!v.saved && f.state === 'ready' && !f.warn))])),
})
