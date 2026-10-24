/* Notebook: the one notebook as its own place (components/Notebook.tsx).
   The place stays mounted, so an unsaved note survives a visit elsewhere. URL: /notebook,
   ?note=<path>[&edit=1|&history=1]. */
import { Notebook as Place } from '../components/Notebook'
import { useNav } from '../lib/nav'

export function Notebook({ active }: { active: boolean }) {
  const nav = useNav()
  const route = nav.route.section === 'notebook' ? nav.route : null
  return <Place active={active} note={route?.note} edit={!!route?.edit} history={!!route?.history} />
}
