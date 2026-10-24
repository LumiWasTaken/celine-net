/* Reminders: the messages it sends later in this conversation (a reminder is an intercom
   message with a time, lib/reminders). Every pending one is a line at the end of the
   transcript (`ReminderLines`), and the head's Reminders (and "+" in the box) opens the
   dialog with all of them, each with Edit and Cancel, and New reminder. The list is
   fetched by the page (GET /intercom?to=&scheduled=true) and again on intercom.state. */
import { useState } from 'react'
import { Bell, Pencil, Plus } from 'lucide-react'
import { stamp } from '../lib/format'
import { intercomApi } from '../lib/intercom'
import { remindersApi, type Reminder } from '../lib/reminders'
import { useAction } from '../lib/useAction'
import { Button, Dialog, EmptyState, EventLine, Field, IconButton, InlineError, Input, ListRow, TextArea } from '../ui'

/** the pending ones, soonest first */
export const pendingOf = (list: Reminder[] | null) =>
  (list ?? []).filter(r => r.state === 'pending').sort((x, y) => Date.parse(x.due_at) - Date.parse(y.due_at))

/** every pending reminder as a quiet line after the messages */
export function ReminderLines({ list }: { list: Reminder[] }) {
  if (!list.length) return null
  return (
    <div className="chat-col chat-events" role="list" aria-label="Reminders">
      {list.map(r => (
        <div key={r.id} role="listitem">
          <EventLine icon={Bell} quiet meta={stamp(Date.parse(r.due_at))}><span title={r.text}>{r.text}</span></EventLine>
        </div>
      ))}
    </div>
  )
}

/** the conversation's reminders: Edit and Cancel on each, New reminder */
export function Reminders({ conversationId, list, reload, onClose }: {
  conversationId: string
  list: Reminder[]
  reload: () => void
  onClose: () => void
}) {
  const [form, setForm] = useState<Reminder | 'new' | null>(list.length ? null : 'new')
  const cancel = useAction(async (id: number) => { await intercomApi.cancel(id); reload() })
  if (form) {
    return <ReminderForm conversationId={conversationId} editing={form === 'new' ? null : form}
                         onClose={() => (list.length ? setForm(null) : onClose())}
                         onDone={() => { reload(); setForm(null) }} />
  }
  return (
    <Dialog title="Reminders" size="sm" onClose={onClose}
            actions={<Button icon={Plus} onClick={() => setForm('new')}>New reminder</Button>}>
      {cancel.error && <InlineError compact message={cancel.error} />}
      {list.length ? list.map(r => {
        const time = stamp(Date.parse(r.due_at))
        return (
          <ListRow key={r.id} icon={Bell} title={r.text} tooltip={r.text} subtitle={time} trailingAlways
                   trailing={<>
                     <IconButton icon={Pencil} size="sm" label="Edit" onClick={() => setForm(r)} />
                     <Button size="sm" variant="ghost" loading={cancel.busy} title={`Cancel the reminder at ${time}`}
                             onClick={() => void cancel.run(r.id)}>Cancel</Button>
                   </>} />
        )
      }) : <EmptyState icon={Bell} />}
    </Dialog>
  )
}

/** New reminder, or Edit: a changed one is set anew and the old one cancelled */
function ReminderForm({ conversationId, editing, onClose, onDone }: {
  conversationId: string
  editing: Reminder | null
  onClose: () => void
  onDone: () => void
}) {
  const [when, setWhen] = useState(editing ? stamp(Date.parse(editing.due_at)).split(', ').at(-1)! : '')
  const [text, setText] = useState(editing?.text ?? '')
  const submit = useAction(async () => {
    await remindersApi.set(conversationId, when, text)
    if (editing) await intercomApi.cancel(editing.id)
    onDone()
  })
  const ready = !!when.trim() && !!text.trim()
  return (
    <Dialog title={editing ? 'Edit' : 'New reminder'} size="sm" onClose={onClose} dismissable={!submit.busy}
            actions={<>
              <Button variant="secondary" onClick={onClose} disabled={submit.busy}>Cancel</Button>
              <Button variant="primary" disabled={!ready} loading={submit.busy} onClick={() => void submit.run()}>
                {editing ? 'Save' : 'Set reminder'}
              </Button>
            </>}>
      <form onSubmit={e => { e.preventDefault(); if (ready) void submit.run() }}>
        <Field label="When">
          <Input autoFocus value={when} placeholder="30 min, or 08:00" onChange={e => setWhen(e.target.value)} />
        </Field>
        <Field label="Text">
          <TextArea rows={2} value={text} onChange={e => setText(e.target.value)} />
        </Field>
        {submit.error && <InlineError compact message={submit.error} />}
      </form>
    </Dialog>
  )
}
