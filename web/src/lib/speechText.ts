/** Speak reply prose, not fenced code, links, markup or provider control tags. */
export function speechChunks(markdown: string): string[] {
  const text = markdown.replace(/```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)/g, '')
    .replace(/<\|[^>]*\|>/g, '').replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/https?:\/\/\S+/g, '')
    .replace(/<[^>]*>/g, '').replace(/^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)/gm, '')
    .replace(/[`*_~]/g, '').trim()
  const result: string[] = []
  let part = ''
  const flush = () => { if (part) result.push(part); part = '' }
  const segmenter = new Intl.Segmenter(undefined, { granularity: 'sentence' })
  for (const line of text.split(/\n+/)) {
    for (const { segment } of segmenter.segment(line)) {
      const sentence = segment.trim()
      if (!sentence) continue
      if (part && part.length + sentence.length + 1 > 900) flush()
      if (sentence.length <= 900) { part = part ? `${part} ${sentence}` : sentence; continue }
      for (const word of sentence.split(/\s+/)) {
        if (part && part.length + word.length + 1 > 900) flush()
        let rest = word
        while (rest.length > 900) { result.push(rest.slice(0, 900)); rest = rest.slice(900) }
        part = part ? `${part} ${rest}` : rest
      }
    }
    flush()
  }
  return result
}

/** Independent lookahead producer, sequential playback; exactly one synthesis in flight. */
export async function playSpeechQueue<T>(texts: string[], options: {
  signal: AbortSignal; generate: (text: string, signal: AbortSignal) => Promise<T>
  play: (value: T, signal: AbortSignal) => Promise<void>
}) {
  const local = new AbortController()
  const signal = AbortSignal.any([options.signal, local.signal])
  const buffer: ({ value: T } | { error: unknown })[] = []
  let done = false
  let wake: (() => void) | undefined
  const notify = () => { wake?.(); wake = undefined }
  signal.addEventListener('abort', notify)
  const producer = (async () => {
    try {
      for (const text of texts) {
        signal.throwIfAborted()
        const value = await options.generate(text, signal)
        signal.throwIfAborted()
        buffer.push({ value }); notify()
      }
    } catch (error) { if (!signal.aborted) buffer.push({ error }) }
    finally { done = true; notify() }
  })()
  try {
    while (!done || buffer.length) {
      signal.throwIfAborted()
      if (!buffer.length) { await new Promise<void>(resolve => { wake = resolve }); continue }
      const item = buffer.shift()!
      if ('error' in item) throw item.error
      await options.play(item.value, signal)
    }
    signal.throwIfAborted()
  } finally {
    local.abort(); signal.removeEventListener('abort', notify); buffer.length = 0
    void producer
  }
}
