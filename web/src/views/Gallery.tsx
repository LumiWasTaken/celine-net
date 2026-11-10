/* Gallery: every image and track it made, and yours, newest first, day by day (GET
   /api/batches, every kind or the Type's): a day's images as a grid of pictures, a click
   opens them full size in the Viewer, which flips through all of them; its tracks as
   rows, a row opens the chat or run it was made in (the batch's `source`, the server's),
   Play plays it in the player at the foot. Batch events put a batch in place as it fills.
   One page, no place sidebar. URL: /gallery. */
import { useState } from 'react'
import { Images, Music, Play, Shapes } from 'lucide-react'
import { useServerEvents } from '../lib/bus'
import { clock, dayLabel } from '../lib/format'
import { batchEvent, fileUrl, mediaApi, tileUrl, withBatch, type Batch, type BatchItem, type Kind } from '../lib/media'
import { madeWords, titleOf } from '../lib/pictures'
import { useResource } from '../lib/useResource'
import { Button, GroupHead, IconButton, ListRow, Loaded, Picture, Player, PlaceHead, Toolbar, VirtualList } from '../ui'
import { PhotoViewer } from '../components/PhotoViewer'
import './Gallery.css'

const PAGE = 60       // items a page shows
const BATCHES = 50    // batches a request asks for
const TILE = 320      // the widest a picture is in the grid (--card-min: 20rem at most)
const KIND_WORD: Record<Kind, string> = { images: 'Images', music: 'Music' }

type Made = { b: Batch; i: BatchItem }
type Day = { day: string; images: Made[]; tracks: Made[] }

const made = (b: Batch): Made[] => b.items.filter(i => i.state === 'done' && i.attachment).map(i => ({ b, i }))
const key = (m: Made) => `${m.b.id}:${m.i.index}`

export function Gallery({ active }: { active: boolean }) {
  const [kind, setKind] = useState<Kind | null>(null)
  // the pages loaded belong to the Type they were loaded for: another starts at one
  const [more, setMore] = useState({ kind, pages: 1 })
  const pages = more.kind === kind ? more.pages : 1
  const list = useResource(async signal => {
    const out = await mediaApi.batches({ kind, limit: BATCHES }, signal)
    while (out.next && out.batches.flatMap(made).length < PAGE * pages) {
      const r = await mediaApi.batches({ kind, limit: BATCHES, before: out.next }, signal)
      out.batches.push(...r.batches)
      out.next = r.next
    }
    return out
  }, [kind, pages], { enabled: active, onReconnect: true })
  useServerEvents(ev => {
    const b = batchEvent(ev, kind ?? undefined)
    if (b) list.set(d => d && { ...d, batches: withBatch(d.batches, b) })
  })
  const [open, setOpen] = useState<number | null>(null)
  const [playing, setPlaying] = useState<Made | null>(null)

  const all = (list.data?.batches ?? []).flatMap(made)
  const days: Day[] = []
  for (const m of all) {
    const day = dayLabel(Date.parse(m.b.created_at))
    if (days.at(-1)?.day !== day) days.push({ day, images: [], tracks: [] })
    days.at(-1)![m.b.kind === 'images' ? 'images' : 'tracks'].push(m)
  }
  const images = days.flatMap(d => d.images)
  const shown = open !== null ? images[open] : undefined

  const draw = (d: Day) => (
    <div className="gallery__day">
      <GroupHead title={d.day} />
      {d.images.length > 0 && (
        <div className="gallery__grid">
          {d.images.map(m => {
            const f = m.i.attachment!
            return <Picture key={key(m)} src={tileUrl(f, TILE)} full={fileUrl(f)} name={titleOf(m.i.prompt)}
                            onOpen={() => setOpen(images.indexOf(m))} />
          })}
        </div>
      )}
      {d.tracks.map(m => {
        const f = m.i.attachment!, title = m.i.title ?? f.filename
        return (
          <ListRow key={key(m)} icon={Music} title={title} subtitle={m.b.source?.title ?? m.i.style} tooltip={m.i.style}
                   meta={clock(Date.parse(m.b.created_at))} href={m.b.source?.link ?? undefined} trailingAlways
                   trailing={<IconButton icon={Play} size="sm" label={`Play ${title}`} onClick={() => setPlaying(m)} />} />
        )
      })}
    </div>
  )

  const things = kind ? KIND_WORD[kind].toLowerCase() : 'images or music'
  return (
    <div className="list-page">
      <PlaceHead level={1} title="Gallery" />
      <div className="list-page__top">
        <Toolbar filters={[{
          label: 'Type', icon: Shapes, picked: kind ? KIND_WORD[kind] : undefined, onClear: () => setKind(null),
          items: (['images', 'music'] as const).map(k => ({ label: KIND_WORD[k], current: k === kind, onSelect: () => setKind(k) })),
        }]} />
      </div>
      <Loaded page res={list} icon={Images} empty={all.length === 0 && { title: `No ${things} yet` }}>
        {data => (
          <div className="list-page__rows">
            <VirtualList items={days} estimate={400} getKey={d => d.day} renderRow={draw} label="Gallery" resetOn={kind}
                         endThreshold={2}
                         onEndReached={() => { if (data.next && !list.loading) setMore({ kind, pages: pages + 1 }) }} />
          </div>
        )}
      </Loaded>
      {playing && (
        <div className="gallery__player">
          <Player key={key(playing)} src={fileUrl(playing.i.attachment!)} title={playing.i.title ?? playing.i.attachment!.filename}
                  detail={playing.b.source?.title ?? undefined} onClose={() => setPlaying(null)} />
        </div>
      )}
      {active && shown && (
        <PhotoViewer src={fileUrl(shown.i.attachment!)} name={titleOf(shown.i.prompt)} prompt={shown.i.prompt}
                     details={madeWords(shown.i)}
                     actions={shown.b.source?.link && (
                       <Button size="sm" variant="ghost" href={shown.b.source.link} onClick={() => setOpen(null)}>
                         {shown.b.source.title}
                       </Button>
                     )}
                     onClose={() => setOpen(null)} index={open!} total={images.length}
                     onPrev={open! > 0 ? () => setOpen(open! - 1) : undefined}
                     onNext={open! < images.length - 1 ? () => setOpen(open! + 1) : undefined} />
      )}
    </div>
  )
}
