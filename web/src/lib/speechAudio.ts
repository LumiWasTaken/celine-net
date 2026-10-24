/** One clip. Autoplay denial waits for a real user gesture without regenerating it. */
export function playSpeechAudio(blob: Blob, signal: AbortSignal, blocked: (resume: (() => void) | null) => void) {
  return new Promise<void>((resolve, reject) => {
    signal.throwIfAborted()
    const url = URL.createObjectURL(blob)
    const audio = new Audio(url)
    let settled = false
    const cleanup = () => {
      settled = true; blocked(null)
      signal.removeEventListener('abort', abort)
      audio.onended = null; audio.onerror = null; audio.pause(); audio.removeAttribute('src')
      URL.revokeObjectURL(url)
    }
    const abort = () => { cleanup(); reject(new DOMException('Stopped', 'AbortError')) }
    const failed = () => { cleanup(); reject(new Error('Audio could not be played.')) }
    const play = () => {
      if (settled) return
      blocked(null)
      void audio.play().catch(e => {
        if (settled) return
        if (e?.name === 'NotAllowedError') blocked(play)
        else failed()
      })
    }
    signal.addEventListener('abort', abort, { once: true })
    audio.onended = () => { cleanup(); resolve() }
    audio.onerror = failed
    play()
  })
}
