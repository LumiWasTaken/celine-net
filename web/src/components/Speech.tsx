import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { Mic, Settings2, Square, Volume2, X } from 'lucide-react'
import { speechApi } from '../lib/speechApi'
import { playSpeechQueue, speechChunks } from '../lib/speechText'
import { playSpeechAudio } from '../lib/speechAudio'
import { usePref } from '../lib/prefs'
import { useNav } from '../lib/nav'
import { useServerEvents } from '../lib/bus'
import { errorText } from '../lib/format'
import { Choice, Dialog, Field, IconButton, Input, Switch } from '../ui'
import './Speech.css'

type Preferences = { voice: string; language: string; send: boolean; autoRead: boolean }
const defaults: Preferences = { voice: '', language: '', send: false, autoRead: false }
type SpeechState = {
  input: boolean; output: boolean; prefs: Preferences; setPrefs: (p: Preferences) => void
  playing: string | null; error: string | null; stop: () => void
  resume: (() => void) | null
  read: (text: string, id: string) => void; visible: (cid: string) => boolean
}
const Context = createContext<SpeechState | null>(null)
export const useSpeech = () => useContext(Context)

export function SpeechProvider({ features, children }: { features?: Record<string, boolean>; children: ReactNode }) {
  const nav = useNav()
  const [stored, setPrefs] = usePref<Preferences>('speech', defaults)
  const prefs = { ...defaults, ...stored }
  const [playing, setPlaying] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [resume, setResume] = useState<(() => void) | null>(null)
  const work = useRef<AbortController | null>(null)
  const input = features?.speech_input === true
  const output = features?.speech_output === true
  const stop = useCallback(() => { work.current?.abort(); work.current = null; setPlaying(null); setResume(null) }, [])
  useEffect(() => { stop(); return stop }, [nav.route, input, output, stop])
  useServerEvents(ev => { if (ev.type === 'settings.changed') stop() })
  const read = useCallback((text: string, id: string) => {
    stop(); setError(null)
    const chunks = speechChunks(text)
    if (!output || !chunks.length) return
    const controller = new AbortController()
    work.current = controller; setPlaying(id)
    void playSpeechQueue(chunks, {
      signal: controller.signal,
      generate: (t, signal) => speechApi.synthesize(t, prefs.voice, signal),
      play: (blob, signal) => playSpeechAudio(blob, signal, retry => setResume(() => retry)),
    }).catch(e => { if (!controller.signal.aborted) setError(errorText(e)) })
      .finally(() => { if (work.current === controller) { work.current = null; setPlaying(null) } })
  }, [output, prefs.voice, stop])
  const visible = (cid: string) => ('conversation' in nav.route && nav.route.conversation === cid)
  return <Context.Provider value={{ input, output, prefs, setPrefs, playing, error, resume, stop, read, visible }}>{children}</Context.Provider>
}

export function SpeechControls({ conversation, active, disabled, onTranscript }: {
  conversation: string | null; active: boolean; disabled: boolean; onTranscript: (text: string, send: boolean) => void
}) {
  const speech = useSpeech()
  const [open, setOpen] = useState(false)
  const [voices, setVoices] = useState<string[]>([])
  const [state, setState] = useState<'idle' | 'permission' | 'recording' | 'transcribing'>('idle')
  const [error, setError] = useState<string | null>(null)
  const task = useRef<AbortController | null>(null)
  const capture = useRef<MediaRecorder | null>(null)
  const latest = useRef(onTranscript)
  latest.current = onTranscript
  const cancel = useCallback(() => {
    task.current?.abort(); task.current = null
    const recorder = capture.current
    capture.current = null
    if (recorder) {
      recorder.onstop = null; recorder.ondataavailable = null; recorder.onerror = null
      if (recorder.state !== 'inactive') recorder.stop()
      recorder.stream.getTracks().forEach(t => t.stop())
    }
    setState('idle')
  }, [])
  useEffect(() => { cancel(); return cancel }, [conversation, active, disabled, speech?.input, cancel])
  useServerEvents(ev => { if (ev.type === 'settings.changed') cancel() })
  useEffect(() => {
    if (!open || !speech?.output) return
    const c = new AbortController()
    void speechApi.voices(c.signal).then(r => { if (!c.signal.aborted) setVoices(r.voices) })
      .catch(e => { if (!c.signal.aborted) setError(errorText(e)) })
    return () => c.abort()
  }, [open, speech?.output])
  const start = async () => {
    cancel(); setError(null)
    if (!navigator.mediaDevices?.getUserMedia || !globalThis.MediaRecorder) {
      setError('Microphone recording needs HTTPS or localhost and a supported browser.'); return
    }
    const controller = new AbortController()
    task.current = controller; setState('permission')
    const preferences = speech!.prefs
    let stream: MediaStream | null = null
    try {
      speech?.stop()
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (controller.signal.aborted) { stream.getTracks().forEach(t => t.stop()); return }
      const mimeType = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4']
        .find(type => MediaRecorder.isTypeSupported(type))
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      capture.current = recorder
      const chunks: Blob[] = []
      let size = 0
      const finish = () => { if (recorder.state !== 'inactive') recorder.stop() }
      const timer = setTimeout(finish, 120_000)
      controller.signal.addEventListener('abort', () => clearTimeout(timer), { once: true })
      recorder.ondataavailable = e => {
        size += e.data.size
        if (size > 12_000_000) { setError('Recording is too large. Try a shorter message.'); cancel(); return }
        if (e.data.size) chunks.push(e.data)
      }
      recorder.onerror = () => { setError('The microphone recording failed.'); cancel() }
      recorder.onstop = () => {
        clearTimeout(timer); recorder.stream.getTracks().forEach(t => t.stop()); capture.current = null
        if (controller.signal.aborted) return
        setState('transcribing')
        const blob = new Blob(chunks, { type: recorder.mimeType })
        if (!blob.size) { cancel(); return }
        void speechApi.transcribe(blob, preferences.language, controller.signal).then(r => {
          if (!controller.signal.aborted && r.text.trim()) latest.current(r.text.trim(), preferences.send)
        }).catch(e => { if (!controller.signal.aborted) setError(errorText(e)) })
          .finally(() => { if (task.current === controller) { task.current = null; setState('idle') } })
      }
      recorder.start(1000); setState('recording')
    } catch (e) {
      stream?.getTracks().forEach(t => t.stop())
      if (!controller.signal.aborted) { setError(errorText(e)); cancel() }
    }
  }
  if (!speech || (!speech.input && !speech.output)) return null
  const p = speech.prefs
  const busy = state !== 'idle'
  return <div className="speech-controls">
    {speech.input && <IconButton icon={state === 'recording' ? Square : Mic}
      label={state === 'recording' ? 'Finish recording' : state === 'idle' ? 'Dictate' : 'Cancel dictation'}
      variant={state === 'recording' ? 'danger' : 'ghost'} disabled={disabled || !active}
      onClick={() => state === 'recording' ? capture.current?.stop() : busy ? cancel() : void start()} />}
    {busy && <IconButton icon={X} label="Cancel dictation" onClick={cancel} />}
    {speech.playing && <IconButton icon={Square} label="Stop playback" onClick={speech.stop} />}
    {speech.resume && <IconButton icon={Volume2} label="Play audio" onClick={speech.resume} />}
    <IconButton icon={Settings2} label="Speech options" pressed={open} onClick={() => setOpen(v => !v)} />
    {(busy || speech.playing || error || speech.error) && <div className="speech-controls__status" role="status">
      {error || speech.error || (state === 'recording' ? 'Recording' : state === 'permission' ? 'Waiting for microphone' : state === 'transcribing' ? 'Transcribing' : speech.resume ? 'Tap Play audio' : 'Reading aloud')}
    </div>}
    {open && <Dialog title="Speech options" onClose={() => setOpen(false)}>
      {speech.input && <>
        <Field label="Language"><Input value={p.language} placeholder="Auto" maxLength={32}
          onChange={e => speech.setPrefs({ ...p, language: e.target.value })} /></Field>
        <Switch checked={p.send} label="Send after transcription" onChange={send => speech.setPrefs({ ...p, send })} />
      </>}
      {speech.output && <>
        <Field label="Voice"><Input value={p.voice} placeholder="Provider default"
          onChange={e => speech.setPrefs({ ...p, voice: e.target.value })} /></Field>
        {voices.length > 0 && <Choice label="Available voices" value={p.voice} onChange={voice => speech.setPrefs({ ...p, voice })}
          options={[{ value: '', label: 'Provider default' }, ...Array.from(new Set([p.voice, ...voices].filter(Boolean))).map(v => ({ value: v, label: v }))]} />}
        <Switch checked={p.autoRead} label="Read new replies automatically" onChange={autoRead => speech.setPrefs({ ...p, autoRead })} />
      </>}
    </Dialog>}
  </div>
}

/** Only a newly completed successful reply, never history loaded on navigation. */
export function useReplySpeech(id: string, cid: string, text: string, running: boolean, done: boolean, last: boolean) {
  const speech = useSpeech()
  const wasRunning = useRef(false)
  useEffect(() => {
    const newlyDone = wasRunning.current && !running && done
    wasRunning.current = running
    if (newlyDone && last && speech?.output && speech.prefs.autoRead && speech.visible(cid)) speech.read(text, id)
  }, [running, done, last, speech, text, id, cid])
  return speech?.output ? <IconButton icon={speech.playing === id ? Square : Volume2} size="sm"
    label={speech.playing === id ? 'Stop playback' : 'Read aloud'}
    onClick={() => speech.playing === id ? speech.stop() : speech.read(text, id)} /> : null
}
