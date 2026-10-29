/* The inline picture block of the chat: ONE block for the whole life of a
   picture (one block). While it is made its shape breathes with the prompt in
   it ("Creating image" while the prompt is still being written, the server's line above
   it while it waits) and Stop; when it failed, the same shape, still, with the server's
   line in plain words and Retry, the item made again as it was asked (Open Settings too
   when it needs the user; stopped, "Paused." and Regenerate); done, the
   picture fills it and a click opens the Viewer, titled by what it shows, its prompt under
   it, preset, size and seed in "...". Under a done picture, on hover,
   focus or tap, a row of reserved height with "..."; a right-click or a long press opens
   the same menu. The chat goes on under it either way. */
import { useContext, type CSSProperties } from 'react'
import { Download, MoreHorizontal, RefreshCw, Settings } from 'lucide-react'
import { api } from '../lib/api'
import { fileUrl, tileUrl } from '../lib/media'

const BLOCK = 640     // the widest a picture block is (.picture-block--wide)
import { useNav } from '../lib/nav'
import { madeWords, needsOwner, titleOf, type PictureState } from '../lib/pictures'
import { useAction } from '../lib/useAction'
import { Button, InlineError, MenuButton, Notice, Picture, cx, toast, type MenuItem } from '../ui'
import { GalleryContext } from './Batch'
import { madeMenu } from './PhotoViewer'
import { settingOf } from './Status'
import './PictureBlock.css'

export function PictureBlock({ pic, onStop, onRetry, className }: {
  pic: PictureState
  /** while it is made: stop it */
  onStop?: () => void
  /** when it failed or was stopped: make it again (left out where nothing can) */
  onRetry?: () => Promise<unknown>
  className?: string
}) {
  const retry = useAction(async () => { await onRetry?.() })
  const nav = useNav()
  const gallery = useContext(GalleryContext)
  const portrait = useAction(async () => { await api.setPortrait(src); toast({ text: 'Portrait set' }) })
  const title = titleOf(pic.prompt)
  const made = madeWords(pic)
  const w = pic.picture?.width ?? pic.width ?? 1, h = pic.picture?.height ?? pic.height ?? 1
  const shape = { '--shape': `${w} / ${h}` } as CSSProperties
  const failed = pic.status === 'failed'
  const paused = failed && pic.line === 'Paused.'
  const making = pic.status === 'running'
  const setting = failed && pic.line ? settingOf(pic.line) : null
  const owner = failed && needsOwner(pic.line)
  const file = pic.picture
  const src = file ? fileUrl(file) : ''
  const items: MenuItem[] = file ? [
    ...madeMenu({ prompt: pic.prompt, details: made, onPortrait: () => void portrait.run() }),
    { label: 'Download', icon: Download, onSelect: () => download(src, file.filename) },
  ] : []

  return (
    <figure className={cx('picture-block', w > h ? 'picture-block--wide' : w < h ? 'picture-block--tall' : 'picture-block--square', className)}
            style={shape}>
      {pic.status === 'done' && file ? <>
        <Picture className="picture-block__open" src={tileUrl(file, BLOCK)} full={src} name={title} ratio={`${w} / ${h}`} more={items}
                 onOpen={gallery ? () => gallery.openImage(file.id, {
                   id: file.id, sha256: file.sha256, filename: file.filename, width: w, height: h,
                   prompt: pic.prompt ?? undefined, preset: pic.preset ?? undefined, seed: pic.seed ?? undefined,
                 }) : undefined}
                 viewer={{ caption: pic.prompt && pic.prompt.split(/\n\s*/).filter(Boolean)
                   .map((line, i) => <p key={i} className="picture-block__caption">{line}</p>) }} />
        <div className="picture-block__acts" role="toolbar">
          <MenuButton icon={MoreHorizontal} label="More" size="sm" items={items} />
        </div>
      </> : (
        <div className={cx('picture-block__box', making && 'is-making')} title={pic.prompt || undefined}>
          {making && <span className="picture-block__shade" aria-hidden />}
          <div className="picture-block__say">
            {failed && pic.line && !paused
              ? <Notice compact tone="error">{pic.line}</Notice>
              : pic.line && <span className="picture-block__line" role="status">{pic.line}</span>}
            {(making || pic.prompt) && (
              <p className="picture-block__prompt" role={making ? 'status' : undefined}>{pic.prompt || 'Creating image'}</p>
            )}
            <span className="picture-block__btns">
              {making && onStop && <Button size="sm" variant="ghost" onClick={onStop}>Stop</Button>}
              {owner && (
                <Button size="sm" icon={Settings} onClick={() => nav.go({ section: 'settings', page: setting?.page ?? 'models' })}>
                  Open Settings
                </Button>
              )}
              {failed && onRetry && (
                <Button size="sm" variant={owner ? 'ghost' : 'secondary'} icon={RefreshCw} loading={retry.busy}
                        onClick={() => void retry.run()}>{paused ? 'Regenerate' : 'Retry'}</Button>
              )}
            </span>
            {(retry.error || portrait.error) && <InlineError compact message={retry.error ?? portrait.error} />}
          </div>
        </div>
      )}
    </figure>
  )
}

function download(src: string, name: string) {
  const a = document.createElement('a')
  a.href = src; a.download = name
  document.body.append(a); a.click(); a.remove()
}
