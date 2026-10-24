/** One clip. Autoplay denial waits for a real user gesture without regenerating it. */
export function playSpeechAudio(blob: Blob, signal: AbortSignal, blocked: (resume: (() => void) | null) => void,
                               audio: HTMLAudioElement) {
  return new Promise<void>((resolve, reject) => {
    signal.throwIfAborted()
    const url = URL.createObjectURL(blob)
    let settled = false
    const cleanup = () => {
      settled = true; blocked(null)
      signal.removeEventListener('abort', abort)
      audio.onended = null; audio.onerror = null; audio.pause(); audio.removeAttribute('src')
      URL.revokeObjectURL(url)
    }
    const abort = () => { cleanup(); reject(new DOMException('Stopped', 'AbortError')) }
    const failed = () => {
      const detail = audio.error
      const message = detail ? `Audio playback failed (media error ${detail.code}${detail.message ? `: ${detail.message}` : ''}).` : 'Audio could not be played.'
      cleanup(); reject(new Error(message))
    }
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
    // Use the page's native player, just like the setup test. Reusing a mounted
    // element also keeps Firefox's media/output state across queued clips.
    audio.src = url
    audio.load()
    play()
  })
}
