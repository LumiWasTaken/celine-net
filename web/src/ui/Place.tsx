/* Place: a place with sections (Documents, the Gallery, Settings, Notebook) is its sidebar
   beside the page. The sidebar (--sidebar-w) holds the place's name with its New as an
   icon, its sections with their counts, then Recent, and the user's own row at its foot;
   it is navigation, not a list pane. Below 1024 px (a tablet, a phone) it is a Drawer from
   the left, opened by the panel button every PlaceHead inside the place shows there; a
   link in it closes it.

     <Place title="Documents" action={<IconButton icon={SquarePen} label="New folder" href=... />}
            side={<><ListRow .../>...</>} foot={<ListRow title="Sam" .../>}>
       <PlaceHead title="Documents" ... />
       ...
     </Place> */
import { createContext, useContext, useState, type ReactNode } from 'react'
import { cx } from './cx'
import { Drawer } from './Drawer'
import { useNarrow } from './phone'

const PlaceCtx = createContext<{ title: string; open: () => void } | null>(null)

/** the place a head sits in (its panel button on a phone), or null */
export const usePlace = () => useContext(PlaceCtx)

export function Place({ title, action, head, side, foot, label, className, open: shown, onOpen, children }: {
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
  /** the page */
  children: ReactNode
}) {
  const narrow = useNarrow()
  const [own, setOwn] = useState(false)
  const open = shown ?? own
  const setOpen = onOpen ?? setOwn
  // a link or a row in the drawer goes somewhere: the drawer gets out of the way
  const closeOnGo = (e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest('a[href], .list-row')) setOpen(false)
  }
  // inside the sidebar a PlaceHead is the sidebar's own: no panel button
  const body = <PlaceCtx.Provider value={null}>
    {head && narrow && head}
    <div className="place-side__body">{side}</div>
    {foot && <div className="place-side__foot">{foot}</div>}
  </PlaceCtx.Provider>
  return (
    <PlaceCtx.Provider value={{ title, open: () => setOpen(true) }}>
      <div className={cx('place', className)}>
        {narrow ? (
          <Drawer side="left" open={open} title={title} actions={action} onClose={() => setOpen(false)} label={label ?? title} fill>
            <div className="place-side place-side--drawer" onClickCapture={closeOnGo}>{body}</div>
          </Drawer>
        ) : (
          <aside className="place-side" aria-label={label ?? title}>
            {head ?? (
              <div className="place-side__head">
                <h2 className="place-side__title">{title}</h2>
                {action}
              </div>
            )}
            {body}
          </aside>
        )}
        <div className="place__main">{children}</div>
      </div>
    </PlaceCtx.Provider>
  )
}
