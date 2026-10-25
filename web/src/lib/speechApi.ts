import { request, requestBlob } from './api'

export type Direction = 'input' | 'output'
/** the model role each direction runs on (server: speech.ROLE) */
export const SPEECH_ROLE = { input: 'transcription', output: 'speech' } as const
export const speechApi = {
  voices: (signal?: AbortSignal) => request<{ voices: string[] }>('/speech/voices', { signal }),
  transcribe: (blob: Blob, language: string, signal: AbortSignal) => {
    const form = new FormData()
    form.append('file', blob, 'recording')
    if (language) form.append('language', language)
    return request<{ text: string }>('/speech/transcriptions', { method: 'POST', body: form, signal })
  },
  synthesize: (text: string, voice: string, signal: AbortSignal) => requestBlob('/speech/clips', {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, voice }),
  }),
}
