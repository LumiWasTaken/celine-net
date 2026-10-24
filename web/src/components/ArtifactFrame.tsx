/* One version of an artifact as its page, in a frame with the same sandbox as the page's
   own header (lib/artifacts SANDBOX). A hairline around the frame marks where the app
   ends and its page begins. */
import { pageUrl, SANDBOX } from '../lib/artifacts'
import './Artifact.css'

export function ArtifactFrame({ id, n, title }: { id: string; n: number; title: string }) {
  return (
    <div className="art__stage">
      <iframe className="art__frame" src={pageUrl(id, n)} sandbox={SANDBOX} allow="" referrerPolicy="no-referrer"
              title={title} />
    </div>
  )
}
