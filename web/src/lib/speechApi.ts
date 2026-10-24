import { request, requestBlob } from './api'
import type { FieldSchema } from './settingsApi'

export type Direction = 'input' | 'output'
export type SpeechEndpoint = { url?: string; model?: string }
export type SpeechSettings = {
  endpoint: SpeechEndpoint; version: string; fields: FieldSchema[]
  key: { state: 'set' | 'not set'; set_at: string | null }
}
export const speechApi = {
  settings: (direction: Direction, signal?: AbortSignal) => request<SpeechSettings>(`/speech/${direction}`, { signal }),
  save: (direction: Direction, endpoint: SpeechEndpoint, version: string, key?: string | null) =>
    request<SpeechSettings>(`/speech/${direction}`, { method: 'PUT', headers: { 'If-Match': version },
      body: JSON.stringify({ endpoint, ...(key === undefined ? {} : { key }) }) }),
  voices: (signal?: AbortSignal) => request<{ voices: string[] }>('/speech/output/voices', { signal }),
  transcribe: (blob: Blob, language: string, signal: AbortSignal) => {
    const form = new FormData()
    form.append('file', blob, 'recording')
    if (language) form.append('language', language)
    return request<{ text: string }>('/transcriptions', { method: 'POST', body: form, signal })
  },
  synthesize: (text: string, voice: string, signal: AbortSignal) => requestBlob('/speech/audio', {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, voice }),
  }),
}
