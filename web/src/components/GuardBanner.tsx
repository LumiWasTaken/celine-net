/* Bastet's banner: one decision of hers that acted (a stop, a freeze, a patrol finding)
   after the message that made the call, as one compact row: its name and its line (the
   server's words, the rule in plain words), its "?" when it has answers, and the answers in reach:
   Unfreeze while its freeze holds, Allow once on a stop or a freeze. Allow for 7 days and Reply (which opens a field in place) sit in the quiet group, shown on
   hover, focus or a tap as a reply's action row is. Under it the thread: your answers and its
   own. What an answer grants is the server's (guard/banners.py); the banner changes by the event
   guard.banner. */
import { useState } from 'react'
import { Ban, CornerUpLeft, Eye, ShieldCheck, Snowflake, type LucideIcon } from 'lucide-react'
import { Button, ComposerBox, Help, ICON, IconButton, InlineError, cx } from '../ui'
import { ACTION_ICONS, ACTION_WORDS, guardApi, type BannerKind, type GuardBanner as Banner } from '../lib/guard'
import { fullDate } from '../lib/format'
import { useAction } from '../lib/useAction'
import './GuardBanner.css'

const ICONS: Record<BannerKind, LucideIcon> = {
  stop: Ban, freeze: Snowflake, approved: ShieldCheck, denied: Ban, closed: Ban, patrol: Eye,
}

export function GuardBanner({ banner: b, user }: { banner: Banner; user: string }) {
  const [replying, setReplying] = useState(false)
  const [text, setText] = useState('')
  const answer = useAction(async (body: Parameters<typeof guardApi.answer>[1]) => {
    await guardApi.answer(b.id, body)
    if ('text' in body) { setText(''); setReplying(false) }
  })
  const [shown, setShown] = useState(false)
  const Icon = ICONS[b.kind]
  return (
    <div className={cx('guard-banner', b.frozen && 'guard-banner--frozen')} data-shown={shown || undefined}
         onClick={e => { if (matchMedia('(hover: none)').matches && !(e.target as HTMLElement).closest('button, a, textarea, [popover]')) setShown(v => !v) }}>
      <div className="guard-banner__row">
        <span className={cx('guard-banner__icon', `guard-banner__icon--${b.kind}`)}>
          <Icon size={ICON.sm} aria-hidden />
        </span>
        {/* a div: the "?" holds its popover, which a <p> cannot */}
        <div className="guard-banner__line">
          <b>Bastet</b> {b.line}
          {b.help && <> <Help text={b.help} label={b.actions.map(a => ACTION_WORDS[a]).join(' and ')} /></>}
        </div>
        <span className="guard-banner__actions">
          {b.frozen && <Button size="sm" icon={Snowflake} loading={answer.busy} onClick={() => void answer.run({ button: 'unfreeze' })}>Unfreeze</Button>}
          {b.actions.includes('once') && (
            <Button size="sm" variant="ghost" icon={ACTION_ICONS.once} disabled={answer.busy} onClick={() => void answer.run({ button: 'once' })}>{ACTION_WORDS.once}</Button>
          )}
          {/* Allow for 7 days and Reply: on hover, focus or a tap, their room kept */}
          <span className="guard-banner__quiet">
            {b.actions.includes('pass') && (
              <IconButton icon={ACTION_ICONS.pass} label={ACTION_WORDS.pass} size="sm" disabled={answer.busy} onClick={() => void answer.run({ button: 'pass' })} />
            )}
            <IconButton icon={CornerUpLeft} label="Reply" size="sm" onClick={() => setReplying(true)} />
          </span>
        </span>
      </div>
      {b.thread.length > 0 && (
        <div className="guard-banner__thread">
          {b.thread.map((t, i) => (
            <p key={i} className="guard-banner__said" title={fullDate(Date.parse(t.at))}>
              <b>{t.by === 'guard' ? 'Bastet' : user}</b> {t.text}
            </p>
          ))}
        </div>
      )}
      {replying && (
        <ComposerBox className="guard-banner__reply" value={text} onChange={setText} autoFocus placeholder="Reply to Bastet"
                     ready={!!text.trim() && !answer.busy} running={answer.busy}
                     onSend={() => void answer.run({ text: text.trim() })}
                     onKeyDown={e => { if (e.key === 'Escape') { setReplying(false); setText('') } }} />
      )}
      {answer.error && <InlineError compact message={answer.error} />}
    </div>
  )
}
