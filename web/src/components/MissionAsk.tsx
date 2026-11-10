/* What a mission asks you (lib/waits MissionWait, the server's): why it waits (its note)
   and what it wrote you, your reply into its conversation (to the newest message that
   waits, so its thread stops waiting, else as a chat message), Mark as done, Stop and
   Dismiss (its messages are read, it works on). On the Waiting page and on the mission's
   own page, whose head has Stop already. */
import { useState } from 'react'
import { CheckCheck, CircleCheck, CircleStop } from 'lucide-react'
import { conversationApi } from '../lib/api'
import { errorText } from '../lib/format'
import { intercomApi } from '../lib/intercom'
import { missionsApi } from '../lib/missions'
import { OFFLINE_TITLE, useOnline } from '../lib/socket'
import { useAction } from '../lib/useAction'
import type { MissionWait } from '../lib/waits'
import { Button, ComposerBox, ConfirmDialog, InlineError } from '../ui'
import './MissionAsk.css'

export function MissionAsk({ w, stop = true }: { w: MissionWait; stop?: boolean }) {
  const online = useOnline()
  const [text, setText] = useState('')
  const [stopping, setStopping] = useState(false)
  const newest = w.asked.at(-1)
  const send = useAction(async () => {
    if (newest) await intercomApi.send({ to: '', text: text.trim(), reply_to: newest })
    else await conversationApi.send(w.id, text.trim(), [])
    setText('')
  })
  const done = useAction(() => missionsApi.done(w.id))
  const dismiss = useAction(() => intercomApi.read(w.asked))
  const error = send.error ?? done.error ?? dismiss.error
  return (
    <div className="mission-ask">
      {w.why && <p className="mission-ask__why">{w.why}</p>}
      {w.text && <p className="mission-ask__text">{w.text}</p>}
      <ComposerBox value={text} onChange={setText} onSend={() => void send.run()} placeholder="Reply"
                   ready={!!text.trim() && !send.busy} disabled={online ? undefined : OFFLINE_TITLE} />
      <div className="mission-ask__actions">
        <Button icon={CircleCheck} loading={done.busy} onClick={() => void done.run()}>Mark as done</Button>
        {stop && <Button icon={CircleStop} onClick={() => setStopping(true)}>Stop</Button>}
        {w.asked.length > 0 && <Button variant="ghost" icon={CheckCheck} loading={dismiss.busy} onClick={() => void dismiss.run()}>Dismiss</Button>}
      </div>
      {error && <InlineError compact message={error} />}
      {stopping && <StopMission cid={w.id} name={w.title} onClose={() => setStopping(false)} />}
    </div>
  )
}

/** the confirm before a mission stops */
export function StopMission({ cid, name, onClose, onDone }: { cid: string; name: string; onClose: () => void; onDone?: () => void }) {
  return (
    <ConfirmDialog title={`Stop ${name}?`} danger confirmLabel="Stop" body="Its chat, plan and terminals are kept."
                   onClose={onClose}
                   onConfirm={async () => { try { await missionsApi.stop(cid) } catch (e) { return errorText(e) } onDone?.(); onClose() }} />
  )
}
