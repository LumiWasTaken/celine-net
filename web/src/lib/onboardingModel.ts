import type { Available, ModelConfig } from './settingsApi'

/** Discovery fills defaults, never removes a saved capability or model override. */
export function onboardingModel(id: string, found?: Available, saved?: ModelConfig): ModelConfig {
  return {
    label: found?.name || id,
    ...(found?.context ? { context: found.context } : {}),
    ...(found?.thinking_levels?.length ? { thinking_levels: found.thinking_levels } : {}),
    ...saved,
    caps: [...new Set([...(saved?.caps ?? []), ...(found?.caps ?? ['chat'])])],
  }
}
