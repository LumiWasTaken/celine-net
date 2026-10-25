import type { Available, ModelConfig } from './settingsApi'

/** Discovery fills defaults; saved capabilities (including disabled ones) win. */
export function onboardingModel(id: string, found?: Available, saved?: ModelConfig): ModelConfig {
  return {
    label: found?.name || id,
    ...(found?.context ? { context: found.context } : {}),
    ...(found?.thinking_levels?.length ? { thinking_levels: found.thinking_levels } : {}),
    ...saved,
    caps: [...(saved?.caps ?? found?.caps ?? ['chat'])],
  }
}
