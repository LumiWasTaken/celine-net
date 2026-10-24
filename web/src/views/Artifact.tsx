/* One of its artifacts as a page of its own, the frame filling the window under the
   header (components/ArtifactView). URL: /artifact/<id>. Loaded lazily. There is no
   conversation here, so Ask Celine about this starts a new one with the draft. */
import { askCeline } from '../lib/ask'
import { useNav } from '../lib/nav'
import { ArtifactView } from '../components/ArtifactView'
import { setPageName } from '../lib/title'

export function ArtifactPage({ id, assistant }: { id: string; assistant: string }) {
  const nav = useNav()
  return (
    <ArtifactView id={id} assistant={assistant} onAsk={(text, refs) => askCeline(nav, text, refs)}
                  onLoaded={a => setPageName(a.title)} />
  )
}
