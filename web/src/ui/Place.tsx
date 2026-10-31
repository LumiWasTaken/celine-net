/* Place: a place with sections (Documents, the Gallery, Settings, Notebook) is its sidebar
   beside the page. The sidebar (--sidebar-w) holds the place's name with its New as an
   icon, its sections with their counts, then Recent, and the user's own row at its foot;
   it is navigation, not a list pane. Below 1024 px it is a Drawer (from the left on a
   tablet, a sheet from the bottom edge on a phone), opened by the panel button every PlaceHead inside the place shows there; a
   link in it closes it. With `fold` the sidebar folds away to the left on a wide screen:
   its head starts with Close sidebar, the page's head with Open sidebar while it is away,
   and this device keeps which. The page takes its new width at once and glides from where
   its middle was, with the sidebar; its heads' actions on the right edge hold still.

     <Place title="Documents" action={<IconButton icon={SquarePen} label="New folder" href=... />}
            side={<><ListRow .../>...</>} foot={<ListRow title="Sam" .../>}>
       <PlaceHead title="Documents" ... />
       ...
     </Place> */
import { createContext, useContext, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { usePref } from '../lib/prefs'
import { cx } from './cx'
import { curve, ms, reduced } from './motion'
import { Drawer } from './Drawer'
import { useNarrow } from './phone'

/** a head's panel button: its label and what it does; `always` shows it at every width */
type Side = { label: string; onClick: () => void; always: boolean }
const PlaceCtx = createContext<Side | null>(null)

/** the panel button of a head in a place, or null */
export const usePlace = () => useContext(PlaceCtx)

export function Place({ title, action, head, side, foot, label, className, open: shown, onOpen, fold, children }: {
  /** the place's name, the sidebar's head */
  title: string
  /** the head's one action: New as an icon */
  action?: ReactNode
  /** a sidebar that is the place's list (Celine's conversations): its own PlaceHead with
      Search, Select, Archive and Trash, in place of the title and action */
  head?: ReactNode
  /** the sections with their counts, ListSections ("Recent") and their rows */
  side: ReactNode
  /** the user's own row */
  foot?: ReactNode
  label?: string
  className?: string
  /** the phone's drawer, when the page decides (a route that shows it open) */
  open?: boolean
  onOpen?: (open: boolean) => void
  /** the sidebar folds away on a wide screen; the key this device keeps it under */
  fold?: string
  /** the page */
  children: ReactNode
}) {
  const narrow = useNarrow()
  const [own, setOwn] = useState(false)
  const open = shown ?? own
  const setOpen = onOpen ?? setOwn
  const [away, setFolded] = usePref(`place:${fold ?? ''}:folded`, false)
  const folded = !!fold && !narrow && away
  // the page's box before the sidebar goes or comes; after, it glides from its middle there
  const main = useRef<HTMLDivElement>(null)
  const was = useRef<DOMRect | null>(null)
  const setAway = (on: boolean) => { was.current = main.current?.getBoundingClientRect() ?? null; setFolded(on) }
  useLayoutEffect(() => {
    const a = was.current, el = main.current
    was.current = null
    if (!a || !el || reduced()) return
    const b = el.getBoundingClientRect()
    const dx = a.left + a.width / 2 - (b.left + b.width / 2)
    // on the sidebar's own curve: in on --dur-slow --ease, out on --dur --ease-exit
    const timing = folded ? { duration: ms('--dur'), easing: curve('--ease-exit') } : { duration: ms('--dur-slow'), easing: curve('--ease') }
    el.animate([{ translate: `${dx}px 0` }, { translate: '0 0' }], timing)
    // a head's actions sit on the right edge, which stays: they hold still
    for (const acts of el.querySelectorAll('.place-head__acts')) {
      acts.animate([{ translate: `${-dx}px 0` }, { translate: '0 0' }], { ...timing, composite: 'add' })
    }
  }, [folded])
  // a link or a row in the drawer goes somewhere: the drawer gets out of the way
  const closeOnGo = (e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest('a[href], .list-row')) setOpen(false)
  }
  // inside the sidebar a PlaceHead is the sidebar's own: Close sidebar where it folds
  const inSide: Side | null = fold && !narrow ? { label: 'Close sidebar', onClick: () => setAway(true), always: true } : null
  const body = <>
    {head && narrow && head}
    <div className="place-side__body">{side}</div>
    {foot && <div className="place-side__foot">{foot}</div>}
  </>
  const page: Side = folded ? { label: 'Open sidebar', onClick: () => setAway(false), always: true }
    : { label: title, onClick: () => setOpen(true), always: false }
  return (
    <PlaceCtx.Provider value={page}>
      <div className={cx('place', folded && 'place--folded', className)}>
        <PlaceCtx.Provider value={inSide}>
          {narrow ? (
            <Drawer side="left" open={open} title={title} actions={action} onClose={() => setOpen(false)} label={label ?? title} fill>
              <div className="place-side place-side--drawer" onClickCapture={closeOnGo}>{body}</div>
            </Drawer>
          ) : (
            <aside className="place-side" aria-label={label ?? title} inert={folded || undefined}>
              {head ?? (
                <div className="place-side__head">
                  <h2 className="place-side__title">{title}</h2>
                  {action}
                </div>
              )}
              {body}
            </aside>
          )}
        </PlaceCtx.Provider>
        <div ref={main} className="place__main">{children}</div>
      </div>
    </PlaceCtx.Provider>
  )
}
