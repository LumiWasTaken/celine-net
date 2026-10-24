import { useCallback, useEffect, useRef, useState } from 'react'
import { speechApi, type Direction } from '../lib/speechApi'
import { errorText } from '../lib/format'
import { Button, Choice, Field, InlineError, Input } from '../ui'

/** Real provider requests, available before opting in. Never submits a chat message. */
export function SpeechTest({ direction, identity, ready, save }: {
  direction: Direction; identity: string; ready: boolean; save: () => Promise<void>
}) {
  const [state, setState] = useState<'idle' | 'saving' | 'permission' | 'recording' | 'testing'>('idle')
  const [error, setError] = useState<string | null>(null)
  const [text, setText] = useState('This is a speech playback test.')
  const [voice, setVoice] = useState('')
  const [voices, setVoices] = useState<string[]>([])
  const [transcript, setTranscript] = useState<string | null>(null)
  const [audio, setAudio] = useState<string | null>(null)
  const work = useRef<AbortController | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const objectUrl = useRef<string | null>(null)
  const stop = useCallback(() => {
    work.current?.abort(); work.current = null
    const r = recorder.current; recorder.current = null
    if (r) {
      r.onstop = null; r.ondataavailable = null; r.onerror = null
      if (r.state !== 'inactive') r.stop()
      r.stream.getTracks().forEach(t => t.stop())
    }
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current)
    objectUrl.current = null; setAudio(null); setState('idle')
  }, [])
  useEffect(() => { stop(); setError(null); setTranscript(null); setVoices([]); return stop }, [identity, stop])
  const begin = async () => {
    stop(); setError(null); setTranscript(null)
    const c = new AbortController(); work.current = c; setState('saving')
    let stream: MediaStream | null = null
    const failed = (e: unknown) => { if (!c.signal.aborted) { setError(errorText(e)); stop() } }
    try {
      await save()
      if (c.signal.aborted) return
      if (direction === 'output') {
        setState('testing')
        const blob = await speechApi.synthesize(text, voice, c.signal, true)
        if (c.signal.aborted) return
        const url = URL.createObjectURL(blob); objectUrl.current = url; setAudio(url); setState('idle')
        return
      }
      if (!navigator.mediaDevices?.getUserMedia || !globalThis.MediaRecorder)
        throw new Error('Microphone recording needs HTTPS or localhost and a supported browser.')
      setState('permission')
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (c.signal.aborted) { stream.getTracks().forEach(t => t.stop()); return }
      const mimeType = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'].find(t => MediaRecorder.isTypeSupported(t))
      const r = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      recorder.current = r
      const chunks: Blob[] = []; let size = 0
      const timer = setTimeout(() => { if (r.state !== 'inactive') r.stop() }, 120_000)
      c.signal.addEventListener('abort', () => clearTimeout(timer), { once: true })
      r.ondataavailable = e => {
        size += e.data.size
        if (size > 12_000_000) { failed(new Error('Recording is too large. Try a shorter sample.')); return }
        if (e.data.size) chunks.push(e.data)
      }
      r.onerror = () => failed(new Error('The microphone recording failed.'))
      r.onstop = () => {
        clearTimeout(timer); r.stream.getTracks().forEach(t => t.stop()); recorder.current = null
        if (c.signal.aborted) return
        setState('testing')
        void speechApi.transcribe(new Blob(chunks, { type: r.mimeType }), '', c.signal, true)
          .then(result => {
            if (c.signal.aborted) return
            if (!result.text.trim()) { failed(new Error('No speech was recognized. Try another sample.')); return }
            setTranscript(result.text); setState('idle')
          }).catch(failed)
      }
      r.start(1000); setState('recording')
    } catch (e) { stream?.getTracks().forEach(t => t.stop()); failed(e) }
  }
  const discoverVoices = async () => {
    stop(); setError(null)
    const c = new AbortController(); work.current = c; setState('saving')
    try {
      await save()
      if (c.signal.aborted) return
      const result = await speechApi.voices(c.signal)
      if (!c.signal.aborted) { setVoices(result.voices); if (!voice && result.voices[0]) setVoice(result.voices[0]); setState('idle') }
    } catch (e) { if (!c.signal.aborted) { setError(errorText(e)); stop() } }
  }
  const busy = state !== 'idle'
  return <>
    {direction === 'output' && <>
      <Field label="Sample text"><Input value={text} maxLength={900} disabled={busy} onChange={e => setText(e.target.value)} /></Field>
      <Field label="Test voice"><Input value={voice} disabled={busy} placeholder="Provider default" onChange={e => setVoice(e.target.value)} /></Field>
      {voices.length > 0 && <Choice label="Available test voices" value={voice} disabled={busy} onChange={setVoice}
        options={Array.from(new Set([voice, ...voices].filter(Boolean))).map(v => ({ value: v, label: v }))} />}
      <Button disabled={!ready || busy} onClick={() => void discoverVoices()}>Discover voices</Button>
    </>}
    <div className="onb__check">
      <Button disabled={!ready || (busy && state !== 'recording') || (direction === 'output' && !text.trim())}
        onClick={() => state === 'recording' ? recorder.current?.stop() : void begin()}>
        {state === 'recording' ? 'Finish and transcribe' : direction === 'input' ? 'Record test' : 'Test speech'}
      </Button>
      {(busy || audio) && <Button onClick={stop}>Stop test</Button>}
    </div>
    {busy && <p role="status">{state === 'recording' ? 'Recording' : state === 'permission' ? 'Waiting for microphone' : state === 'saving' ? 'Saving settings' : direction === 'input' ? 'Transcribing sample' : 'Generating sample'}</p>}
    {transcript !== null && <Field label="Recognized speech"><p role="status">{transcript}</p></Field>}
    {audio && <Field label="Generated sample"><audio key={audio} controls autoPlay src={audio} onError={() => setError('The browser could not play this audio.')} /></Field>}
    {error && <InlineError compact message={error} />}
  </>
}
