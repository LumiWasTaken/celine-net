/* One artifact in the kit's detail pattern, in the side panel and as the full page: the head
   with its title, the version menu ("v3, 14:02", each version's change note as a muted line),
   Restore this version for an older one, Download (what it wrote, as a file) and Ask Celine
   about this; More holds Move to Trash (or Restore). Below it the page in its sandboxed
   frame, or a document rendered as its replies are. The agent's next version replaces the one on
   screen unless you picked an older one. */
import { useEffect, useState, type ReactNode } from 'react'
import { AppWindow, ChevronLeft, Download, MessageSquarePlus, RotateCcw, Trash2 } from 'lucide-react'
import { ApiError } from '../lib/api'
import { artifactEvent, artifactsApi, kindWord, sourceUrl, type Artifact } from '../lib/artifacts'
import { stamp } from '../lib/format'
import { format } from '../lib/nav'
import { useAction } from '../lib/useAction'
import { useResource } from '../lib/useResource'
import { Button, EmptyState, IconButton, InlineError, Loaded, LoadingRow, MenuButton, PlaceHead, Section } from '../ui'
import { ArtifactFrame } from './ArtifactFrame'
import { Markdown } from './Markdown'
import './Artifact.css'

export function ArtifactView({ id, assistant, onAsk, onBack, extra, onLoaded }: {
  id: string
  /** its name, for Ask Celine about this */
  assistant: string
  /** Ask Celine about this: the words and the chip, for the draft */
  onAsk: (text: string, refs: string[]) => void
  /** in the side panel: back to the list */
  onBack?: () => void
  /** more head buttons (the panel's Open in a new tab) */
  extra?: ReactNode
  onLoaded?: (a: Artifact) => void
}) {
  const res = useResource(signal => artifactsApi.get(id, signal).catch(e => {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }), [id], { events: artifactEvent(id), resetOnChange: true })
  // the version on screen: null follows the newest
  const [picked, setPicked] = useState<number | null>(null)
  useEffect(() => setPicked(null), [id])
  const a = res.data
  useEffect(() => { if (a) onLoaded?.(a) }, [a]) // eslint-disable-line react-hooks/exhaustive-deps
  const restore = useAction(async (n: number) => {
    res.set(await artifactsApi.restore(id, n))
    setPicked(null)
  })
  const trash = useAction(async (trashed: boolean) => { res.set(await artifactsApi.setTrashed(id, trashed)) })

  const back = onBack && <IconButton icon={ChevronLeft} label="Back to the list" onClick={onBack} />
  if (a === null) {
    // in the panel the head's back arrow leads out; the full page offers its place instead
    return (
      <div className="art">
        {back && <PlaceHead level={1} lead={back} title="" />}
        <div className="art__empty">
          <EmptyState icon={AppWindow} title="Not found" action={!back && <Button href={format({ section: 'celine' })}>Open {assistant}</Button>} />
        </div>
      </div>
    )
  }
  if (!a) {
    return (
      <div className="art">
        <PlaceHead level={1} lead={back} title="" />
        {res.error ? <div className="art__error"><InlineError message={res.error} onRetry={res.reload} /></div> : <LoadingRow />}
      </div>
    )
  }
  const n = picked ?? a.n
  const word = kindWord(a.kind)
  const file = `${a.title.replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-|-$/g, '') || word}.${a.kind === 'html' ? 'html' : 'md'}`
  const failed = restore.error ?? trash.error
  // the full page leads back to the conversation it was made in
  const lead = back || (a.conversation_id
    ? <IconButton icon={ChevronLeft} label="Open conversation" href={format({ section: 'celine', conversation: a.conversation_id })} />
    : undefined)
  return (
    <div className="art">
      <PlaceHead level={1} className="art__head" lead={lead} title={a.title} titleAttr={a.title}
                 detail={a.trashed_at ? 'Trash' : undefined}
                 actions={<>
                   {n !== a.n && <Button variant="primary" icon={RotateCcw} loading={restore.busy} onClick={() => void restore.run(n)}>Restore this version</Button>}
                   <MenuButton label="Versions" text={version(n, a)}
                               items={[...a.versions].reverse().map(v => ({
                                 label: version(v.n, a), detail: v.note || undefined, title: v.note || undefined, current: v.n === n,
                                 onSelect: () => setPicked(v.n === a.n ? null : v.n),
                               }))} />
                   <IconButton icon={Download} label={`Download v${n}`} href={sourceUrl(a.id, n)} download={file} />
                   <IconButton icon={MessageSquarePlus} label={`Ask ${assistant} about this`}
                               onClick={() => onAsk(`About this ${word}: `, [`artifact:${a.id}`])} />
                   {extra}
                 </>}
                 more={a.trashed_at
                   ? [{ label: 'Restore', icon: RotateCcw, onSelect: () => void trash.run(false) }]
                   : [{ label: 'Move to Trash', icon: Trash2, danger: true, onSelect: () => void trash.run(true) }]} />
      {failed && <div className="art__error"><InlineError compact message={failed} /></div>}
      {a.kind === 'html'
        ? <ArtifactFrame id={a.id} n={n} title={a.title} />
        : <Doc id={a.id} n={n} />}
    </div>
  )
}

/* "v4, 18:04": the version and when it was made */
function version(n: number, a: Artifact): string {
  const v = a.versions.find(x => x.n === n)
  return v ? `v${n}, ${stamp(v.created_at)}` : `v${n}`
}

/* a markdown document: its text, rendered by the app as its replies are; no frame, no script */
function Doc({ id, n }: { id: string; n: number }) {
  const text = useResource(() => artifactsApi.source(id, n), [id, n])
  return (
    <div className="page">
      <div className="page__col page__col--prose">
        <Loaded res={text}>{t => <Section><article className="prose"><Markdown text={t} /></article></Section>}</Loaded>
      </div>
    </div>
  )
}
