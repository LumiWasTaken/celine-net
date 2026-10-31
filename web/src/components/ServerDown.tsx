import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useServerDown } from '../lib/socket'
import { Dots, EmptyState } from '../ui'

/* The server is gone: one block over the whole app, rendered once (App.tsx). Everything
   underneath is inert, so no click or key reaches it. It goes away by itself when the
   server answers again. When it shows: lib/health.ts and useServerDown. */
export function ServerDown() {
  const down = useServerDown()
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!down) return
    const others = [...document.body.children].filter(el => el !== box.current && !el.hasAttribute('inert'))
    for (const el of others) el.setAttribute('inert', '')
    box.current?.focus()
    return () => { for (const el of others) el.removeAttribute('inert') }
  }, [down])
  if (!down) return null
  return createPortal(
    <div ref={box} className="server-down" role="alertdialog" aria-modal="true" aria-labelledby="server-down-title" tabIndex={-1}>
      <EmptyState title={<h1 id="server-down-title" className="server-down__title">No connection to the server</h1>}>
        Reconnecting<Dots className="server-down__dots" />
      </EmptyState>
    </div>,
    document.body,
  )
}
