/* The connection part of the Discord and WhatsApp pages: the connection's state
   (GET /api/sources/{sid}/connector, fresh on source.state events and every few seconds
   while a login is in progress). */
import { useEffect } from 'react'
import { kbApi, type ConnectorState } from '../../lib/kb'
import { useResource, type Resource } from '../../lib/useResource'

/** the connection's state; while it waits for a login it looks again every 3 seconds
    (a new QR code, a scan) */
export function useConnector(kid: string, active: boolean): Resource<ConnectorState> {
  const r = useResource(() => kbApi.connector(kid), [kid, active], {
    events: ev => (ev.type === 'source.state' || ev.type === 'kb.changed') && ev.kb === kid,
  })
  const waiting = active && r.data?.connection.state === 'needs_login'
  const { reload } = r
  useEffect(() => {
    if (!waiting) return
    const t = setInterval(reload, 3000)
    return () => clearInterval(t)
  }, [waiting, reload])
  return r
}
