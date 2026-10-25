/* WhatsApp: the connection's state in the head (a linked device; a QR code to connect the
   phone, then new messages as they happen), what to fix (a lost connection, a failed
   task), what is included, and the chats you deleted and ignored, each with when. */
import { useState } from 'react'
import { kbApi, type ConnectorState } from '../../lib/kb'
import { errorText, stamp } from '../../lib/format'
import { useOnline } from '../../lib/socket'
import { connectionState, lostWhen, type Source } from '../../lib/sources'
import { useResource } from '../../lib/useResource'
import { Button, InlineError, ListRow, LoadingRow, Section, VirtualList } from '../../ui'
import { useConnector } from './Connector'
import { Page, StatusDot } from './parts'
import { Problem, StateNotice, ToFix, failedLines, includedSentence, scheduleProblems, useSchedules, useTasks } from './SourcePage'
import './settings.css'

export function WhatsAppSource({ source, active }: { source: Source; active: boolean }) {
  const kb = source.kb
  const connector = useConnector(kb.id, active)
  const tasks = useTasks()
  const schedules = useSchedules()

  const st = connector.data
  const list = tasks.data ?? []

  if (connector.error && !st) {
    return <Page title={source.title}><InlineError message={connector.error} onRetry={connector.reload} /></Page>
  }
  if (!st) return <Page title={source.title}><LoadingRow label="Checking the connection" /></Page>

  const c = st.connection
  const state = connectionState('whatsapp', c, st.account?.name ?? null, st.login_lost_at)
  const pairing = c.state === 'needs_login' && <Pairing kid={kb.id} st={st} />

  const lost = c.state === 'needs_login' && !!st.login_lost_at
  return (
    <Page title={source.title} detail={<StatusDot level={state.level} word />}>
      {c.state !== 'needs_login' && <StateNotice level={state.level} sentence={state.sentence} />}

      <ToFix>
        {lost && (
          <Problem key="pair" sentence={`Your phone unlinked WhatsApp ${lostWhen(st.login_lost_at!)}. Connect your phone again.`}>
            {pairing}
          </Problem>
        )}
        {scheduleProblems(kb.id, schedules.data, ['feed'])}
        {failedLines(kb.id, list, tasks.reload)}
      </ToFix>

      {c.state === 'needs_login' && !lost && <Section title="Login">{pairing}</Section>}

      {(kb.messages ?? 0) > 0 && (
        <Section title="Included">
          <p className="src-line">{includedSentence(kb)}</p>
        </Section>
      )}

      <Ignored kid={kb.id} />
    </Page>
  )
}

/** connecting the phone: how, and the QR code once WhatsApp sent one (the onboarding too) */
export function Pairing({ kid, st }: { kid: string; st: ConnectorState }) {
  return (
    <>
      <p className="src-line">On your phone, open WhatsApp, Settings, Linked devices, Link a device. Scan this code.</p>
      <div className="muted">WhatsApp does not allow unofficial clients and may ban the number; use it at your own risk.</div>
      {st.qr_at
        ? <div className="qr"><img src={kbApi.qrUrl(kid, st.qr_at)} alt="QR code to connect WhatsApp" /></div>
        : <LoadingRow inline label="Waiting for a code from WhatsApp" />}
    </>
  )
}

/* Chats deleted permanently from the trash (deleted and ignored). Restore brings back what the linked
   device still has of the chat (a task). Shown only when there are some. */
function Ignored({ kid }: { kid: string }) {
  const online = useOnline()
  const ignored = useResource(() => kbApi.ignoredChats(kid).then(r => r.chats), [kid],
                              { events: ev => ev.type === 'kb.changed' && ev.kb === kid })
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const chats = ignored.data ?? []
  if (!chats.length && !ignored.error) return null

  const restore = async (key: string) => {
    setBusy(key); setError(null)
    try { await kbApi.restoreChat(kid, key); ignored.reload() } catch (e) { setError(errorText(e)) } finally { setBusy(null) }
  }
  return (
    <Section title="Ignored chats" count={chats.length || undefined}>
      {ignored.error && <InlineError compact message={ignored.error} onRetry={ignored.reload} />}
      {error && <InlineError compact message={error} />}
      {chats.length > 0 && (
        <div className="rows">
          <VirtualList items={chats} estimate={44} getKey={c => c.key} page
                       renderRow={c => <ListRow title={c.name} meta={stamp(c.at)} trailingAlways={busy === c.key}
                                                trailing={<Button size="sm" variant="ghost" loading={busy === c.key} disabled={!online || busy !== null}
                                                                  onClick={() => void restore(c.key)}>Restore</Button>} />} />
        </div>
      )}
    </Section>
  )
}
