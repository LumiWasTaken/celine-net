/* A batch of images or music, in the batch's order, which never changes. An image is the
   one picture block (PictureBlock) from the prompt while it is made to the picture or the
   sentence why it failed; a track its title and player, or the server's words. Used in its
   replies (ToolRow ToolLines) and the Generate surface on the Models page; both follow the
   batch's events and never guess an item's state. */
import { createContext, useState } from 'react'
import type { Message, ToolCall } from '../lib/api'
import { asBatch, fileUrl, mediaApi, type Batch, type BatchItem } from '../lib/media'
import { madeWords, pictureOf, retryItem } from '../lib/pictures'
import { useAction } from '../lib/useAction'
import { useOnline } from '../lib/socket'
import { Button, InlineError } from '../ui'
import { PictureBlock } from './PictureBlock'
import './Batch.css'

/** an image to show full size: one it made (with what it was made from), a browser shot */
export type Made = {
  id: string; sha256: string; filename: string; width: number; height: number
  prompt?: string; preset?: string; seed?: number
}
/** "anima-turbo, 832 x 1216, seed 1234" */
export const madeDetails = (img: Made): string | null => madeWords(img)

/** a done image item as an image to show */
function madeOf(b: Batch, i: BatchItem): Made | null {
  const a = i.attachment
  if (b.kind !== 'images' || i.state !== 'done' || !a) return null
  return { id: a.id, sha256: a.sha256, filename: a.filename, width: a.width ?? i.width ?? 0,
           height: a.height ?? i.height ?? 0, prompt: i.prompt, preset: i.preset, seed: i.seed }
}
export const madeImages = (b: Batch) => b.items.flatMap(i => madeOf(b, i) ?? [])

/* In a conversation, a click opens the conversation's own viewer, which flips through
   every image made there (Celine.tsx). */
type GalleryContextValue = {
  images: { img: Made }[]
  openImage: (idOrSha: string, fallback?: Made) => void
}
export const GalleryContext = createContext<GalleryContextValue | null>(null)

/** every image made in a conversation, in order (a read of a batch shows it again: once) */
export function collectGeneratedImages(messages: Message[], toolCalls: Record<string, ToolCall[]>): { img: Made }[] {
  const seen = new Set<string>()
  const out: { img: Made }[] = []
  for (const m of messages) {
    for (const c of toolCalls[m.id] ?? []) {
      const b = asBatch(c.display)
      for (const img of b ? madeImages(b) : []) {
        if (!seen.has(img.sha256)) { seen.add(img.sha256); out.push({ img }) }
      }
    }
  }
  return out
}

/** "2 of 4": how many are done */
export const doneWords = (b: Batch) => `${b.items.filter(i => i.state === 'done').length} of ${b.items.length}`

export function StopBatch({ batch }: { batch: Batch }) {
  const online = useOnline()
  const stop = useAction(() => mediaApi.stop(batch.id))
  if (batch.state !== 'running') return null
  return (
    <>
      <Button size="sm" variant="ghost" loading={stop.busy} disabled={!online} onClick={() => void stop.run()}>Stop</Button>
      {stop.error && <InlineError compact message={stop.error} />}
    </>
  )
}

/** a batch's pictures or tracks */
export function BatchGallery({ batch: initialBatch }: { batch: Batch }) {
  const [optimistic, setOptimistic] = useState<Batch | null>(null)
  const batch = (optimistic && optimistic.id === initialBatch.id && initialBatch.state !== 'running') ? optimistic : initialBatch
  const stop = useAction(() => mediaApi.stop(batch.id))
  const handleRetry = async (index: number) => {
    setOptimistic({
      ...batch,
      state: 'running',
      items: batch.items.map(it => (it.state === 'failed' || it.state === 'stopped')
        ? { ...it, state: 'waiting', error: undefined, seconds: undefined, note: undefined }
        : it),
    })
    try {
      const updated = await retryItem(batch.id, index)
      setOptimistic(updated)
    } catch (e) {
      setOptimistic(null)
      throw e
    }
  }
  if (batch.kind === 'images') {
    const items = batch.items
    return (
      <div className="batch batch--images">
        {items.map(i => (
          <PictureBlock key={i.index} pic={pictureOf(i)}
                        onRetry={batch.state !== 'running' ? () => handleRetry(i.index) : undefined}
                        onStop={batch.state === 'running' ? () => void stop.run() : undefined} />
        ))}
        {stop.error && <InlineError compact message={stop.error} />}
      </div>
    )
  }
  return <div className="batch batch--music">{batch.items.map(i => <Track key={i.index} item={i} />)}</div>
}

/* a track: its title and player once done; the server's words while it waits or runs,
   or why it failed */
function Track({ item: i }: { item: BatchItem }) {
  return (
    <div className={`batch__tile batch__tile--${i.state}`}>
      {i.state === 'done' && i.attachment ? (
        <div className="batch__track">
          <span className="batch__title" title={i.style}>{i.title ?? i.attachment.filename}</span>
          <audio controls preload="metadata" src={fileUrl(i.attachment)} />
        </div>
      ) : (
        <span className="batch__words">{i.state === 'failed' ? i.error : i.state === 'stopped' ? 'Stopped' : i.note}</span>
      )}
    </div>
  )
}
