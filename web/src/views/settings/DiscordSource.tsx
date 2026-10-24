/* Discord: the state in the head, what to fix (a login, a failed task), the login (Sync
   now, the token), then ONE list: its
   first row is your direct messages (none until picked, "Choose chats" picks them or
   leaves them out again), then one row per server with an Include switch. Switching a server
   on opens the pop-up with its channels first; its confirm starts the sync. "Choose
   channels" on an included server opens the same pop-up. The account
   lives in the Discord connection (discord-link); the list merges /discord/targets
   (names, icons, DMs) and /discord/subscriptions. */
import { useCallback, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { KeyRound, LogOut, QrCode, RefreshCw } from 'lucide-react'
import {
  kbApi, type ConnectorState, type DiscordGuild, type DiscordSubSettings,
  type DiscordSubscription, type DiscordTarget,
} from '../../lib/kb'
import { count, countdown, errorText, nothingMatches } from '../../lib/format'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { connectionState, lostWhen, type Source } from '../../lib/sources'
import type { FieldSchema } from '../../lib/settingsApi'
import { useAction } from '../../lib/useAction'
import { useNow } from '../../lib/useNow'
import { useResource, type Resource } from '../../lib/useResource'
import {
  Avatar, Button, Checkbox, Chip, ConfirmDialog, Dialog, EmptyState, Field, InlineError, Input, LoadingRow,
  SearchField, Section, Switch, VirtualList,
} from '../../ui'
import { useConnector } from './Bridge'
import { Page, SchemaForm, StatusDot, TaskLine } from './parts'
import { Problem, StateLine, StateNotice, ToFix, failedLines, scheduleProblems, useSchedules, useTasks } from './SourcePage'
import './settings.css'

export function DiscordSource({ source, active }: { source: Source; active: boolean }) {
  const kb = source.kb
  const online = useOnline()
  const connector = useConnector(kb.id, active)
  const tasks = useTasks()
  const schedules = useSchedules()
  const sync = useAction(() => kbApi.discordSync(kb.id))

  const st = connector.data
  const list = tasks.data ?? []
  // a sync you started: one line under the sentence until dismissed (failed ones go to To fix)
  const checks = list.filter(t => t.kb === kb.id && t.kind === 'sync' && t.state !== 'failed')
  const syncing = checks.find(t => t.state === 'running')

  if (connector.error && !st) {
    return <Page title={source.title}><InlineError message={connector.error} onRetry={connector.reload} /></Page>
  }
  if (!st) return <Page title={source.title}><LoadingRow label="Checking the connection" /></Page>

  const c = st.connection
  const state = connectionState('discord', c, st.account?.name ?? null, st.login_lost_at)
  const lost = c.state === 'needs_login' && !!st.login_lost_at

  return (
    <Page title={source.title} detail={<StatusDot level={state.level} word />}>
      {c.state !== 'needs_login' && <StateNotice level={state.level} sentence={state.sentence} />}

      <ToFix>
        {lost && <LoginProblem key="login" kid={kb.id} st={st} onChanged={connector.reload} />}
        {scheduleProblems(kb.id, schedules.data, ['sync', 'feed'])}
        {failedLines(kb.id, list.filter(t => !/^(subscribe|channels):/.test(t.kind)), tasks.reload)}
      </ToFix>

      <Section title="Login">
        {c.state === 'connected' && (
          <StateLine action={
            <Button icon={RefreshCw} loading={sync.busy || !!syncing} disabled={!online}
                    title={online ? undefined : OFFLINE_TITLE}
                    onClick={() => void sync.run()}>{syncing ? 'Syncing' : 'Sync now'}</Button>
          }>{state.sentence}</StateLine>
        )}
        {checks.map(t => <TaskLine key={t.id} task={t} onChanged={tasks.reload} />)}
        {sync.error && <InlineError compact message={sync.error} />}
        {c.state === 'needs_login' && !lost && <LoginProblem kid={kb.id} st={st} onChanged={connector.reload} />}
        <Account kid={kb.id} stored={!!st.account} onDone={connector.reload} />
      </Section>

      {st.account && <Included kid={kb.id} />}
    </Page>
  )
}

/* ── Included: one list, direct messages first, then the servers ── */

function Included({ kid }: { kid: string }) {
  const targets = useResource(() => kbApi.discordTargets(kid), [kid],
                              { events: ev => ev.type === 'kb.changed' && ev.kb === kid })
  const subs = useResource(() => kbApi.discordSubscriptions(kid), [kid],
                           { events: ev => ev.type === 'kb.changed' && ev.kb === kid })
  const [choosing, setChoosing] = useState(false)

  if (targets.error && !targets.data) return <InlineError message={targets.error} onRetry={targets.reload} />
  if (!targets.data) return <LoadingRow label="Loading" />
  const dms = targets.data.dms
  const left = dms.filter(d => !d.sync).length
  const dmRow = (
    <div className="server">
      <div className="server__row">
        <span className="server__spacer" />
        <Avatar name="Direct messages" seed="dms" size={28} kind="dm" />
        <span className="server__name server__name--on">Direct messages</span>
        <span className="server__meta">{count(dms.length - left, 'chat')}</span>
        {dms.length > 0 && <Button size="sm" onClick={() => setChoosing(true)}>Choose chats</Button>}
      </div>
    </div>
  )

  return (
    <>
      <Servers kid={kid} guilds={targets.data.guilds} subs={subs} first={dmRow} />
      {choosing && <ChooseDms kid={kid} dms={dms} onClose={() => setChoosing(false)} onSaved={targets.reload} />}
    </>
  )
}

/* The direct messages: a filterable list; not included = left out (never synced, not in
   the archive's future). Including one again syncs it from the start. Saves on Save. */
function ChooseDms({ kid, dms, onClose, onSaved }: {
  kid: string; dms: DiscordTarget[]; onClose: () => void; onSaved: () => void
}) {
  const online = useOnline()
  const tasks = useTasks()
  const [keep, setKeep] = useState<Record<string, boolean>>(() => Object.fromEntries(dms.map(d => [d.id, d.sync])))
  const [q, setQ] = useState('')
  const save = useAction(async () => {
    const changed = Object.fromEntries(dms.filter(d => keep[d.id] !== d.sync).map(d => [d.id, !!keep[d.id]]))
    if (Object.keys(changed).length) {
      const r = await kbApi.setDiscordTargets(kid, changed)
      if (r.task) tasks.reload()
    }
    onSaved(); onClose()
  })
  const shown = useMemo(() => {
    const s = q.trim().toLowerCase()
    return s ? dms.filter(d => d.name.toLowerCase().includes(s)) : dms
  }, [dms, q])
  return (
    <Dialog title="Direct messages" onClose={onClose} size="md" dismissable={!save.busy}
            actions={<Button variant="primary" loading={save.busy} disabled={!online} title={online ? undefined : OFFLINE_TITLE}
                             onClick={() => void save.run()}>Save</Button>}>
      <div className="dialog__form">
        <SearchField value={q} onChange={setQ} placeholder="Search direct messages" autoFocus />
        <div className="boxed-list boxed-list--dialog">
          {shown.length === 0 ? <EmptyState compact>{nothingMatches(q)}</EmptyState> : (
            <VirtualList items={shown} estimate={44} getKey={d => d.id} resetOn={q}
                         renderRow={d => (
                           <label className="pick-row">
                             <Checkbox checked={!!keep[d.id]} ariaLabel={`Include ${d.name}`}
                                       onChange={v => setKeep(k => ({ ...k, [d.id]: v }))} />
                             <Avatar name={d.name} src={d.icon} seed={d.id} size={28} kind={d.kind === 'group' ? 'group' : 'dm'} />
                             <span className="pick-row__name">{d.name}</span>
                             <span className="pick-row__meta">{d.messages != null ? count(d.messages, 'message') : ''}</span>
                           </label>
                         )} />
          )}
        </div>
        {save.error && <InlineError compact message={save.error} />}
      </div>
    </Dialog>
  )
}

/* One row per server: included ones first, then the rest by name. The order is fixed
   when the list loads, so a row never jumps away from the switch you just pressed.
   Switching a server on opens its channels first; it syncs once you confirm. */
function Servers({ kid, guilds, subs, first }: {
  kid: string
  guilds: DiscordGuild[]
  /** the direct messages row, first in the same list */
  first: ReactNode
  subs: Resource<{ subscriptions: DiscordSubscription[]; defaults: DiscordSubSettings }>
}) {
  const online = useOnline()
  const tasks = useTasks()
  const [q, setQ] = useState('')
  const [open, setOpen] = useState<DiscordGuild | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<{ guild: string; message: string } | null>(null)
  const byId = useMemo(() => new Map((subs.data?.subscriptions ?? []).map(s => [s.guild_id, s])), [subs.data])
  const order = useRef<string[] | null>(null)
  if (!order.current && subs.data) {
    order.current = [...guilds].sort((a, b) => {
      const sa = byId.get(a.id)?.state === 'on' ? 0 : 1, sb = byId.get(b.id)?.state === 'on' ? 0 : 1
      return sa - sb || a.name.localeCompare(b.name)
    }).map(g => g.id)
  }
  const ordered = useMemo(() => {
    const pos = new Map((order.current ?? []).map((id, i) => [id, i]))
    return [...guilds].sort((a, b) => (pos.get(a.id) ?? 1e9) - (pos.get(b.id) ?? 1e9) || a.name.localeCompare(b.name))
  }, [guilds, subs.data]) // eslint-disable-line react-hooks/exhaustive-deps
  const f = q.trim().toLowerCase()
  const shown = f ? ordered.filter(g => g.name.toLowerCase().includes(f)) : ordered
  const rows: (DiscordGuild | null)[] = [null, ...shown]

  const keep = (sub: DiscordSubscription) =>
    subs.set(d => d && { ...d, subscriptions: [...d.subscriptions.filter(s => s.guild_id !== sub.guild_id), sub] })
  const stop = useCallback(async (g: DiscordGuild) => {
    setBusy(g.id); setError(null)
    try { await kbApi.deleteDiscordSubscription(kid, g.id); subs.reload() }
    catch (e) { setError({ guild: g.id, message: errorText(e) }) }
    finally { setBusy(null) }
  }, [kid, subs])
  /* the pop-up's confirm: start a server that is off, or save the one that runs */
  const confirm = async (g: DiscordGuild, settings: DiscordSubSettings) => {
    const body = { settings, guild_name: g.name, icon: g.icon ?? undefined }
    const r = byId.get(g.id)?.state === 'on'
      ? await kbApi.putDiscordSubscription(kid, g.id, body)
      : await kbApi.startDiscordSubscription(kid, g.id, body)
    keep(r.subscription)
    if (r.task) tasks.reload()
    setOpen(null)
  }

  return (
    <Section title="Included"
             search={guilds.length > 8 ? { value: q, onChange: setQ, label: 'Search servers' } : undefined}>
      {subs.error && !subs.data && <InlineError message={subs.error} onRetry={subs.reload} />}
      {!subs.data && guilds.length > 0 ? <LoadingRow label="Loading servers" /> : (
        <div className="server-list" style={{ '--rows': rows.length } as CSSProperties}>
          <VirtualList items={rows} estimate={44} getKey={(g, i) => g?.id ?? `dms:${i}`} resetOn={q}
                       after={shown.length === 0 ? <EmptyState compact>{q ? `No server matches "${q}".` : 'No servers on this account.'}</EmptyState> : undefined}
                       renderRow={g => {
                         if (!g) return first
                         const sub = byId.get(g.id)
                         const on = sub?.state === 'on'
                         const task = (tasks.data ?? []).filter(t => t.kb === kid && (t.kind === `subscribe:${g.id}` || t.kind === `channels:${g.id}`))
                           .sort((a, b) => b.started_at - a.started_at)[0]
                         const disabled = busy === g.id || !online
                         return (
                           <div className="server">
                             <div className="server__row">
                               <Switch hideLabel label={`Include ${g.name}`} checked={on || open?.id === g.id} disabled={disabled}
                                       title={online ? undefined : OFFLINE_TITLE}
                                       onChange={next => { if (on && !next) void stop(g); else if (next) setOpen(g) }} />
                               <Avatar name={g.name} src={g.icon} seed={g.id} size={28} kind="group" />
                               <span className={on ? 'server__name server__name--on' : 'server__name'}>{g.name}</span>
                               <span className="server__meta">
                                 {on && sub ? count(sub.settings.channels.length, 'channel') : ''}
                               </span>
                               {on && <Button size="sm" disabled={disabled} onClick={() => setOpen(g)}>Choose channels</Button>}
                             </div>
                             {(task || error?.guild === g.id) && (
                               <div className="server__body">
                                 {task && <TaskLine task={task} onChanged={tasks.reload} />}
                                 {error?.guild === g.id && <InlineError compact message={error.message} />}
                               </div>
                             )}
                           </div>
                         )
                       }} />
        </div>
      )}
      {open && subs.data && (
        <ChannelsDialog kid={kid} guild={open} on={byId.get(open.id)?.state === 'on'}
                        initial={byId.get(open.id)?.settings ?? subs.data.defaults}
                        onClose={() => setOpen(null)} onConfirm={s => confirm(open, s)} />
      )}
    </Section>
  )
}

/* A server's settings, as a schema for the generic form (discord-link's policy.DEFAULTS
   without the channels, which the list above them chooses). */
const SERVER_OPTIONS: FieldSchema[] = [
  { key: 'backfill', label: 'Past messages to sync', type: 'choice',
    choices: [['none', 'None'], ['30d', 'Last 30 days'], ['all', 'All']] },
  { key: 'include_attachments', label: 'Sync attachments', type: 'bool' },
  { key: 'include_bots', label: 'Sync bot messages', type: 'bool' },
]

type ChannelRow = { type: 'cat'; name: string; ids: string[] } | { type: 'ch'; c: DiscordTarget }

/* The pop-up: the server's channels by category, a checkbox each and one per category.
   Included channels are synced, nothing else; a channel that appeared since the last save
   is marked New. Its confirm starts the server (or saves it when it runs). */
function ChannelsDialog({ kid, guild, on, initial, onClose, onConfirm }: {
  kid: string
  guild: DiscordGuild
  on: boolean
  initial: DiscordSubSettings
  onClose: () => void
  onConfirm: (s: DiscordSubSettings) => Promise<void>
}) {
  const online = useOnline()
  const channels = useResource(() => kbApi.discordChannels(kid, guild.id).then(r => r.channels),
                               [kid, guild.id])
  const [values, setValues] = useState<DiscordSubSettings>(initial)
  const [q, setQ] = useState('')
  const chosen = useMemo(() => new Set(values.channels), [values.channels])
  const save = useAction(() => onConfirm(values))
  const f = q.trim().toLowerCase()
  const rows = useMemo(() => {
    const by = new Map<string, DiscordTarget[]>()
    for (const c of channels.data ?? []) {
      if (f && !c.name.toLowerCase().includes(f)) continue
      const cat = c.category ?? ''
      if (!by.has(cat)) by.set(cat, [])
      by.get(cat)!.push(c)
    }
    const out: ChannelRow[] = []
    for (const [cat, chs] of by) {
      out.push({ type: 'cat', name: cat, ids: chs.map(c => c.id) })
      for (const c of chs) out.push({ type: 'ch', c })
    }
    return out
  }, [channels.data, f])
  const setMany = (ids: string[], tick: boolean) => setValues(v => {
    const next = new Set(v.channels)
    for (const id of ids) { if (tick) next.add(id); else next.delete(id) }
    return { ...v, channels: [...next] }
  })
  const known = new Set((channels.data ?? []).map(c => c.id))
  const ticked = values.channels.filter(id => known.has(id)).length
  return (
    <Dialog title={guild.name} onClose={onClose} size="md" dismissable={!save.busy}
            actions={<Button variant="primary" loading={save.busy} disabled={!online || !channels.data || (!on && ticked === 0)}
                             title={online ? undefined : OFFLINE_TITLE}
                             onClick={() => void save.run()}>{on ? 'Save' : `Include ${count(ticked, 'channel')}`}</Button>}>
      <div className="dialog__form">
        <SearchField value={q} onChange={setQ} placeholder="Search channels" autoFocus />
        <div className="boxed-list boxed-list--dialog">
          {channels.error ? <InlineError message={channels.error} onRetry={channels.reload} />
            : !channels.data ? <LoadingRow inline label="Loading channels" />
            : rows.length === 0 ? <EmptyState compact>{q ? `No channel matches "${q}".` : 'This server has no text channels you can read.'}</EmptyState>
            : (
              <VirtualList items={rows} estimate={i => (rows[i]?.type === 'cat' ? 40 : 36)}
                           getKey={(r, i) => r.type === 'ch' ? r.c.id : `cat:${r.name}:${i}`} resetOn={f}
                           renderRow={r => {
                             if (r.type === 'cat') {
                               const n = r.ids.filter(id => chosen.has(id)).length
                               return (
                                 <label className="pick-row channels__cat">
                                   <Checkbox checked={n === r.ids.length} indeterminate={n > 0 && n < r.ids.length}
                                             ariaLabel={`All of ${r.name || 'No category'}`} onChange={v => setMany(r.ids, v)} />
                                   <span className="pick-row__name">{r.name || 'No category'}</span>
                                 </label>
                               )
                             }
                             return (
                               <label className="pick-row channels__row">
                                 <Checkbox checked={chosen.has(r.c.id)} ariaLabel={r.c.name} onChange={v => setMany([r.c.id], v)} />
                                 <span className="pick-row__name">{r.c.name}</span>
                                 {r.c.new && <Chip tone="accent">New</Chip>}
                               </label>
                             )
                           }} />
            )}
        </div>
        <SchemaForm schema={SERVER_OPTIONS} values={values as unknown as Record<string, unknown>} disabled={save.busy}
                    onChange={(k, v) => setValues(s => ({ ...s, [k]: v }))} />
        {save.error && <InlineError compact message={save.error} />}
      </div>
    </Dialog>
  )
}

/* ── logging in: a QR code for the Discord app; the token stays in the Discord connection ── */
const ACTIVE = new Set(['starting', 'waiting', 'scanned'])

export function LoginProblem({ kid, st, onChanged }: { kid: string; st: ConnectorState; onChanged: () => void }) {
  const online = useOnline()
  const session = st.login ?? null
  const active = !!session && ACTIVE.has(session.state)
  const now = useNow(active)
  const start = useAction(async () => { await kbApi.discordLoginStart(kid); onChanged() })
  const cancel = useAction(async () => { if (session) { await kbApi.discordLoginCancel(kid, session.id); onChanged() } })

  if (active && session) {
    const left = session.expires_at ? session.expires_at - now : null
    return (
      <Problem calm={!st.login_lost_at} sentence={session.state === 'scanned' ? `Confirm the login on your phone${session.user ? ` (${session.user.name})` : ''}.` : 'Scan this code with the Discord app.'}
               detail={session.state === 'scanned' ? undefined : 'In the Discord app: Settings, Scan QR code, then confirm.'}
               action={{ label: 'Cancel', onClick: () => void cancel.run(), busy: cancel.busy }}>
        {session.state !== 'scanned' && (session.qr_svg
          ? <div className="qr" aria-label="QR code for Discord login" dangerouslySetInnerHTML={{ __html: session.qr_svg }} />
          : <LoadingRow inline label="Getting a code from Discord" />)}
        {left !== null && session.state !== 'scanned' && (
          <div className="muted">{left > 0 ? `This code expires in ${countdown(left)}.` : 'This code has expired.'}</div>
        )}
      </Problem>
    )
  }
  const failed = session && (session.state === 'failed' || session.state === 'expired')
  return (
    <Problem calm={!st.login_lost_at} sentence={st.login_lost_at ? `Discord ended the login ${lostWhen(st.login_lost_at)}.` : undefined}
             detail={st.connection.error ?? undefined}
             action={online ? { label: failed ? 'Get a new code' : 'Log in with QR code', icon: QrCode, onClick: () => void start.run(), busy: start.busy } : undefined}>
      <div className="muted">Logging in with a user account may break Discord's terms; use it at your own risk.</div>
      {failed && <InlineError compact message={session.error ?? (session.state === 'expired' ? 'The code expired before you scanned it.' : 'Discord ended the login without a reason.')} />}
      {start.error && <InlineError compact message={start.error} />}
    </Problem>
  )
}

function Account({ kid, stored, onDone }: { kid: string; stored: boolean; onDone: () => void }) {
  const [confirm, setConfirm] = useState(false)
  const [setting, setSetting] = useState(false)
  return (
    <>
      <div className="row-actions">
        {stored && <Button icon={LogOut} onClick={() => setConfirm(true)}>Remove current Discord token</Button>}
        <Button icon={KeyRound} onClick={() => setSetting(true)}>Set new Discord token</Button>
      </div>
      {confirm && (
        <ConfirmDialog title="Remove the Discord token?" confirmLabel="Remove" danger
                       body="Sync stops until you set a new token or log in again. The token keeps working on Discord."
                       onClose={() => setConfirm(false)}
                       onConfirm={async () => {
                         try { await kbApi.discordLogout(kid); setConfirm(false); onDone() } catch (e) { return errorText(e) }
                       }} />
      )}
      {setting && <TokenDialog kid={kid} onClose={() => setSetting(false)} onDone={onDone} />}
    </>
  )
}

function TokenDialog({ kid, onClose, onDone }: { kid: string; onClose: () => void; onDone: () => void }) {
  const [token, setToken] = useState('')
  const online = useOnline()
  const save = useAction(async () => { await kbApi.discordSetToken(kid, token.trim()); onClose(); onDone() })
  return (
    <Dialog title="Set new Discord token" onClose={onClose} closeButton={false}
            actions={<>
              <Button variant="secondary" onClick={onClose}>Cancel</Button>
              <Button variant="primary" loading={save.busy} disabled={!online || !token.trim()}
                      onClick={() => void save.run()}>Save</Button>
            </>}>
      <form onSubmit={e => { e.preventDefault(); if (online && token.trim()) void save.run() }}>
        <Field label="Token" error={save.error}>
          <Input type="password" autoComplete="off" spellCheck={false} autoFocus value={token}
                 placeholder="Paste the token" onChange={e => setToken(e.target.value)} />
        </Field>
      </form>
    </Dialog>
  )
}
