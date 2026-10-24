/* Message and ComposerBox: a conversation's two parts, the same in every place that has one
   (Celine's chat, an Intercom thread, a bridged chat).

   Message: one message. Its own on a warm surface with its face beside it, yours as a bubble
   at the right. Under it
   the action row (its time, the versions, the caller's buttons and "..."), whose height is
   always kept: it shows on hover, on focus and on a tap, so nothing moves. Right-click and
   a long press open the same menu as its "...".

   ComposerBox: the one raised object at the foot of a conversation: the files on their way
   in, the text box, then a bar with the caller's buttons (the plus, Attach) at the left and
   Send at the right; Send becomes Stop while it answers. Enter sends, Shift+Enter is a new
   line. */
import { useState, type KeyboardEvent, type ReactNode, type Ref } from 'react'
import { ChevronLeft, ChevronRight, MoreHorizontal } from 'lucide-react'
import { IconButton, SendButton } from './Button'
import { cx } from './cx'
import { TextArea } from './Form'
import { MenuButton, useContextMenu, type MenuItem } from './Menu'

/** one rule for every composer: Enter sends, Shift+Enter is a new line (and Enter while an
    input method composes a word does nothing) */
export const isSendKey = (e: KeyboardEvent) => e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing

export function Message({ mine = false, face, name, head, before, children, time, versions, actions, menu, source, shown: keep = false, className }: {
  mine?: boolean
  face?: ReactNode
  /** who wrote it, where the place shows names (a thread) */
  name?: ReactNode
  /** beside the name: to whom */
  head?: ReactNode
  /** above the text: pictures and files on it */
  before?: ReactNode
  /** the text */
  children?: ReactNode
  /** in the action row: when it was written */
  time?: ReactNode
  /** one of several versions (Regenerate): the arrows and "2 of 3" in the action row */
  versions?: { at: number; total: number; onPrev: () => void; onNext: () => void }
  /** the caller's buttons in the action row (Copy, Regenerate) */
  actions?: ReactNode
  /** its "..." and right-click menu */
  menu?: MenuItem[]
  /** where it came from (a mission, a chat): under it, always shown */
  source?: ReactNode
  /** the action row shown without a hover (the last reply on a phone, its menu open) */
  shown?: boolean
  className?: string
}) {
  const ctx = useContextMenu()
  const [shown, setShown] = useState(false)
  const right = menu && { onContextMenu: (e: React.MouseEvent) => ctx.open(e, menu, 'Message'), ...ctx.press(() => menu, 'Message') }
  return (
    <div className={cx('ui-msg', 'ui-msg--chat', mine ? 'ui-msg--mine' : 'ui-msg--theirs', face != null && 'ui-msg--face', className)}
         data-shown={shown || keep || undefined} {...right}
         onClick={e => { if (matchMedia('(hover: none)').matches && !(e.target as HTMLElement).closest('button, a')) setShown(v => !v) }}>
      {face != null && <span className="ui-msg__face">{face}</span>}
      <div className="ui-msg__main">
        {(name != null || head != null) && (
          <div className="ui-msg__head">
            {name != null && <b className="ui-msg__name">{name}</b>}
            {head}
          </div>
        )}
        {before && <div className="ui-msg__before">{before}</div>}
        {children != null && <div className="ui-msg__body">{children}</div>}
        {source && <div className="ui-msg__source">{source}</div>}
        <div className="ui-msg__acts">
          {time != null && <span className="ui-msg__time">{time}</span>}
          {versions && versions.total > 1 && (
            <span className="ui-msg__versions">
              <IconButton icon={ChevronLeft} label="Previous version" size="sm" disabled={versions.at <= 1} onClick={versions.onPrev} />
              <span className="ui-msg__versions-n">{versions.at} of {versions.total}</span>
              <IconButton icon={ChevronRight} label="Next version" size="sm" disabled={versions.at >= versions.total} onClick={versions.onNext} />
            </span>
          )}
          {actions}
          {menu && <MenuButton icon={MoreHorizontal} label="More" items={menu} size="sm" />}
        </div>
      </div>
      {ctx.menu}
    </div>
  )
}

export function ComposerBox({ value, onChange, onSend, onStop, running = false, ready, placeholder, label, start, tools, files,
                              disabled, inputRef, onKeyDown, onPaste, autoFocus, className }: {
  value: string
  onChange: (value: string) => void
  onSend: () => void
  onStop?: () => void
  running?: boolean
  /** something to send; the text by default */
  ready?: boolean
  placeholder?: string
  /** the text box's name for screen readers; the placeholder by default */
  label?: string
  /** buttons at the bar's left: the plus menu, Attach */
  start?: ReactNode
  /** buttons before Send (a Model picker) */
  tools?: ReactNode
  /** the files and pictures on their way in, above the text */
  files?: ReactNode
  /** why it cannot send now (offline): Send's tip */
  disabled?: string
  inputRef?: Ref<HTMLTextAreaElement>
  onKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void
  onPaste?: (e: React.ClipboardEvent<HTMLTextAreaElement>) => void
  autoFocus?: boolean
  className?: string
}) {
  const can = (ready ?? value.trim().length > 0) && !disabled
  return (
    <div className={cx('composer-box', className)}>
      {files && <div className="composer-box__files">{files}</div>}
      <TextArea ref={inputRef} bare rows={1} max={13} value={value} placeholder={placeholder} aria-label={label ?? placeholder}
                className="composer-box__text" autoFocus={autoFocus} onPaste={onPaste}
                onChange={e => onChange(e.target.value)}
                onKeyDown={e => {
                  onKeyDown?.(e)
                  if (e.defaultPrevented) return
                  if (isSendKey(e)) { e.preventDefault(); if (can && !running) onSend() }
                }} />
      <div className="composer-box__bar">
        {start}
        <span className="composer-box__gap" />
        {tools}
        <SendButton ready={can} running={running} onSend={onSend} onStop={onStop} title={disabled} />
      </div>
    </div>
  )
}
