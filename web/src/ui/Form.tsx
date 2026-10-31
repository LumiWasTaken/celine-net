/* Form controls: Field (label + hint + error around one control), Input, TextArea,
   SearchField, Choice, Checkbox, Switch, and Form (a whole form drawn from the server's
   field schema, server/celine/settings/form.py). No dropdowns anywhere: a choice is a
   segmented row or a list. */
import {
  forwardRef, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type InputHTMLAttributes, type ReactNode, type Ref,
  type TextareaHTMLAttributes,
} from 'react'
import { Check, Copy, LoaderCircle, Search, X, type LucideIcon } from 'lucide-react'
import { num } from '../lib/format'
import { Button, IconButton } from './Button'
import { Filters, Segmented, moveRadio } from './Bits'
import { cx } from './cx'
import { coarse } from './phone'
import { ICON } from './icons'
import { InlineError } from './Notice'
import { copied } from './Toast'

/* ── Field ── */
export function Field({ label, hint, error, children, className, group = false }: {
  label?: ReactNode
  hint?: ReactNode
  /** shown under the control, replaces nothing, never a toast */
  error?: string | null
  children: ReactNode
  className?: string
  /** the control is a set of buttons (a Choice): a group, not a <label>, so a click on
      the label picks nothing */
  group?: boolean
}) {
  const inner = <>
    {label && <span className="field-group__label">{label}</span>}
    {children}
    {hint && <span className="field-group__hint">{hint}</span>}
    {error && <InlineError compact message={error} />}
  </>
  // a <label> wrapper names the control inside it without ids
  return group
    ? <div role="group" className={cx('field-group', className)}>{inner}</div>
    : <label className={cx('field-group', className)}>{inner}</label>
}

/* ── Input ── */
type InputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'size'> & {
  size?: 'sm' | 'md'
  mono?: boolean
  invalid?: boolean
}
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { size = 'md', mono, invalid, className, ...rest }, ref,
) {
  return <input ref={ref} className={cx('input', size === 'sm' && 'input--sm', mono && 'input--mono', className)}
                aria-invalid={invalid || undefined} {...rest} />
})

/* ── PinInput: a PIN as one box per digit (8 by default). Typing fills a box and moves on,
   Backspace empties it and goes back, Left and Right move, a paste fills from the first
   box; only digits are kept. `value` is the digits typed so far. ── */
export function PinInput({ value, onChange, length = 8, label, invalid, autoFocus, disabled }: {
  value: string
  onChange: (value: string) => void
  length?: number
  /** accessible name of the group */
  label: string
  invalid?: boolean
  autoFocus?: boolean
  disabled?: boolean
}) {
  const boxes = useRef<(HTMLInputElement | null)[]>([])
  const digits = value.replace(/\D/g, '').slice(0, length)
  const focus = (i: number) => boxes.current[Math.max(0, Math.min(length - 1, i))]?.focus()
  const set = (next: string) => onChange(next.replace(/\D/g, '').slice(0, length))
  return (
    <div className="pin" role="group" aria-label={label}>
      {Array.from({ length }, (_, i) => (
        <input key={i} ref={el => { boxes.current[i] = el }} className="input pin__box" type="password" inputMode="numeric"
               autoComplete="one-time-code" aria-label={`Digit ${i + 1}`} aria-invalid={invalid || undefined}
               value={digits[i] ?? ''} disabled={disabled} autoFocus={autoFocus && i === 0}
               onChange={e => {
                 const typed = e.target.value.replace(/\D/g, '')
                 if (!typed) return
                 set(digits.slice(0, i) + typed + digits.slice(i + typed.length))
                 focus(i + typed.length)
               }}
               onPaste={e => {
                 e.preventDefault()
                 const pasted = e.clipboardData.getData('text')
                 set(pasted)
                 focus(pasted.replace(/\D/g, '').length)
               }}
               onKeyDown={e => {
                 if (e.key === 'Backspace') {
                   e.preventDefault()
                   const at = digits[i] ? i : i - 1
                   if (at >= 0) { set(digits.slice(0, at) + digits.slice(at + 1)); focus(at) }
                 } else if (e.key === 'ArrowLeft') focus(i - 1)
                 else if (e.key === 'ArrowRight') focus(i + 1)
               }} />
      ))}
    </div>
  )
}

/* ── TextArea: the one text box. Its text sets its height, from `rows` lines up to `max`
   lines (never past 60% of the screen), and only then it scrolls; a value set from outside
   sizes it the same way (CSS field-sizing, ui.css .textbox). `bare` drops the input look
   for a box that draws its own (a composer); `fill` leaves the size to its own class (an
   editor that fills its pane). ── */
type TextAreaProps = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'rows'> & {
  rows?: number
  max?: number
  bare?: boolean
  fill?: boolean
  mono?: boolean
  invalid?: boolean
}
const SIZES_ITSELF = typeof CSS !== 'undefined' && CSS.supports('field-sizing', 'content')
export const TextArea = forwardRef<HTMLTextAreaElement, TextAreaProps>(function TextArea(
  { rows = 3, max = 20, bare, fill, mono, invalid, className, style, ...rest }, ref,
) {
  const own = useRef<HTMLTextAreaElement | null>(null)
  // a browser without field-sizing (Safari): size it from its text after each render
  useLayoutEffect(() => {
    const el = own.current
    if (!el || fill || SIZES_ITSELF) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`
  })
  const setRef = (el: HTMLTextAreaElement | null) => {
    own.current = el
    if (typeof ref === 'function') ref(el)
    else if (ref) ref.current = el
  }
  return <textarea ref={setRef} rows={rows} aria-invalid={invalid || undefined}
                   className={cx(!fill && 'textbox', !fill && !bare && 'input', mono && 'input--mono', className)}
                   style={fill ? style : { '--rows': rows, '--max': Math.max(rows, max), ...style } as CSSProperties}
                   {...rest} />
})

/* ── SearchField: the filter box above a list ── */
type SearchFieldProps = {
  value: string
  onChange: (value: string) => void
  placeholder?: string
  /** accessible name; defaults to the placeholder */
  label?: string
  /** the magnifier turns into a spinner; nothing else moves */
  loading?: boolean
  size?: 'sm' | 'md'
  autoFocus?: boolean
  className?: string
  inputRef?: Ref<HTMLInputElement>
  onKeyDown?: (e: React.KeyboardEvent<HTMLInputElement>) => void
  /** the list this box drives with the arrow keys: its id and the active option's id */
  listbox?: { id: string; active?: string }
}
export function SearchField({
  value, onChange, placeholder, label, loading = false, size = 'md',
  autoFocus, className, inputRef, onKeyDown, listbox,
}: SearchFieldProps) {
  const s = size === 'sm' ? ICON.sm : ICON.md
  return (
    <label className={cx('field', size === 'sm' && 'field--sm', className)}>
      {loading ? <LoaderCircle size={s} className="spin" aria-hidden /> : <Search size={s} aria-hidden />}
      <input ref={inputRef} value={value} placeholder={placeholder} aria-label={label ?? placeholder}
             autoFocus={autoFocus && !coarse()}
             {...(listbox && { role: 'combobox', 'aria-expanded': true, 'aria-controls': listbox.id,
                               'aria-activedescendant': listbox.active })}
             onChange={e => onChange(e.target.value)}
             onKeyDown={e => {
               onKeyDown?.(e)
               if (e.defaultPrevented) return
               if (e.key === 'Escape' && value) { e.preventDefault(); e.stopPropagation(); onChange('') }
               else if (e.key === 'Escape') e.currentTarget.blur()
             }} />
      {value && (
        <button type="button" className="field__clear" aria-label="Clear" title="Clear"
                onClick={() => onChange('')}>
          <X size={ICON.sm} aria-hidden />
        </button>
      )}
    </label>
  )
}

/* ── Choice: one of several. Up to six short options (40 letters in all) are a segmented
   row; more are a list with the chosen one marked (`list`: always). `row` keeps any set
   in one flat line that scrolls sideways (Filters: a filter above a list, a target). ── */
type ChoiceOption<V extends string = string> = {
  value: V
  label: string
  disabled?: boolean
  /** a colour option: the colour of its round swatch (a token: "var(--char-rose)") */
  swatch?: string
  /** a person option: their Face */
  face?: ReactNode
  /** a picture option (a variant, a portrait): its src; the label is its name and tip */
  picture?: string
}
/** a labelled group of options, a small heading in the list */
type ChoiceGroup<V extends string = string> = { group: string; options: ChoiceOption<V>[] }
export function Choice<V extends string = string>({
  value, onChange, options, label, row = false, list = false, disabled, className, title, ratio = '1',
}: {
  value: V
  onChange: (value: V) => void
  options: (ChoiceOption<V> | ChoiceGroup<V>)[]
  /** accessible name */
  label: string
  row?: boolean
  list?: boolean
  disabled?: boolean
  className?: string
  title?: string
  /** picture options: their shape ("3 / 4") */
  ratio?: string
}) {
  const flat = options.flatMap(o => 'group' in o ? o.options : [o])
  if (flat.some(o => o.swatch || o.face || o.picture)) {
    return <Tiles value={value} onChange={onChange} options={flat} label={label} disabled={disabled} className={className} ratio={ratio} />
  }
  if (row) return <Filters value={value} onChange={onChange} options={flat} label={label} disabled={disabled} className={className} />
  const short = !list && flat.length <= 6 && options.every(o => !('group' in o)) && flat.reduce((n, o) => n + o.label.length, 0) <= 40
  if (short) {
    return <Segmented value={value} onChange={onChange} options={flat} label={label} disabled={disabled}
                      className={className} title={title} />
  }
  const item = (o: ChoiceOption<V>) => (
    <button key={o.value} type="button" role="radio" aria-checked={o.value === value} className="choice-list__item"
            tabIndex={o.value === value ? 0 : -1} disabled={disabled || o.disabled} onClick={() => onChange(o.value)}>
      <span>{o.label}</span>
      {o.value === value && <Check size={ICON.sm} aria-hidden />}
    </button>
  )
  return (
    <div className={cx('choice-list', className)} role="radiogroup" aria-label={label} title={title}
         onKeyDown={e => { if (!disabled) moveRadio(e, flat.map(o => o.value), value, onChange) }}>
      {options.map(o => 'group' in o ? (
        <div key={o.group} role="presentation" className="choice-list__group-wrap">
          <span className="choice-list__group">{o.group}</span>
          {o.options.map(item)}
        </div>
      ) : item(o))}
    </div>
  )
}

/* Choice as tiles: colours as round swatches, people as faces, pictures as thumbnails; the
   chosen one ringed in the accent and checked. Swatches and faces show their name under them,
   a picture only in its tip. */
function Tiles<V extends string>({ value, onChange, options, label, disabled, className, ratio }: {
  value: V; onChange: (v: V) => void; options: ChoiceOption<V>[]; label: string; disabled?: boolean; className?: string; ratio: string
}) {
  const kind = options.some(o => o.picture) ? 'picture' : options.some(o => o.face) ? 'face' : 'swatch'
  return (
    <div className={cx('choice-tiles', `choice-tiles--${kind}`, className)} role="radiogroup" aria-label={label}
         style={{ '--tile-ratio': ratio } as CSSProperties}
         onKeyDown={e => { if (!disabled) moveRadio(e, options.map(o => o.value), value, onChange) }}>
      {options.map(o => (
        <button key={o.value} type="button" role="radio" aria-checked={o.value === value} className="choice-tile"
                tabIndex={o.value === value ? 0 : -1} disabled={disabled || o.disabled} title={o.label}
                onClick={() => onChange(o.value)}>
          {o.picture ? <img className="choice-tile__pic" src={o.picture} alt={o.label} draggable={false} />
            : o.face ? <span className="choice-tile__face">{o.face}</span>
            : <span className="choice-tile__swatch" style={{ background: o.swatch }} />}
          {o.value === value && <span className="choice-tile__check" aria-hidden><Check size={ICON.xs} /></span>}
          {!o.picture && <span className="choice-tile__label">{o.label}</span>}
        </button>
      ))}
    </div>
  )
}

/* ── Picks: pictures chosen in order, up to `max`; each chosen one carries its number
   (1 for the first picked), and a tap takes it out again. Choice's picture tiles. ── */
export function Picks({ values, onChange, options, label, max, ratio = '1' }: {
  values: string[]; onChange: (v: string[]) => void; options: { value: string; label: string; picture: string }[]
  label: string; max: number; ratio?: string
}) {
  const toggle = (v: string) => onChange(values.includes(v) ? values.filter(x => x !== v)
    : max === 1 ? [v] : values.length < max ? [...values, v] : values)
  return (
    <div className="choice-tiles choice-tiles--picture" role="group" aria-label={label} style={{ '--tile-ratio': ratio } as CSSProperties}>
      {options.map(o => {
        const n = values.indexOf(o.value) + 1
        return (
          <button key={o.value} type="button" role="checkbox" aria-checked={n > 0} className="choice-tile" title={o.label}
                  onClick={() => toggle(o.value)}>
            <img className="choice-tile__pic" src={o.picture} alt={o.label} draggable={false} />
            {n > 0 && <span className="choice-tile__check" aria-hidden>{max === 1 ? <Check size={ICON.xs} /> : n}</span>}
          </button>
        )
      })}
    </div>
  )
}

/* ── FileButton: pick files from the device (Upload, Attach). A
   Button, or an icon button with `label`, over the one hidden file input. ── */
export function FileButton({ accept, multiple = false, onFiles, children, icon, label, variant, size, disabled, className, title }: {
  accept?: string
  multiple?: boolean
  onFiles: (files: File[]) => void
  /** the words; left out it is an icon button named by `label` */
  children?: ReactNode
  icon?: LucideIcon
  label?: string
  variant?: 'primary' | 'secondary' | 'ghost'
  size?: 'sm' | 'md'
  disabled?: boolean
  className?: string
  title?: string
}) {
  const input = useRef<HTMLInputElement>(null)
  const pick = () => input.current?.click()
  return (
    <>
      {children == null && icon
        ? <IconButton icon={icon} label={label ?? ''} variant={variant} size={size} disabled={disabled} className={className} title={title} onClick={pick} />
        : <Button icon={icon} variant={variant} size={size} disabled={disabled} className={className} title={title} onClick={pick}>{children}</Button>}
      <input ref={input} type="file" hidden accept={accept} multiple={multiple} tabIndex={-1}
             onChange={e => { const files = [...(e.target.files ?? [])]; e.target.value = ''; if (files.length) onFiles(files) }} />
    </>
  )
}

/* ── Slider: a value on a range (Bias). The label on the left, the track, the
   value in tabular figures at the right. ── */
export function Slider({ label, value, onChange, min = 0, max = 1, step = 0.01, format, disabled, className }: {
  label: ReactNode
  value: number
  onChange: (value: number) => void
  min?: number
  max?: number
  step?: number
  /** the value as words ("0.6", "Close") */
  format?: (value: number) => ReactNode
  disabled?: boolean
  className?: string
}) {
  const pct = max > min ? ((value - min) / (max - min)) * 100 : 0
  return (
    <label className={cx('slider', className)}>
      <span className="slider__label">{label}</span>
      <input type="range" className="slider__track" min={min} max={max} step={step} value={value} disabled={disabled}
             style={{ '--p': `${pct}%` } as CSSProperties} onChange={e => onChange(Number(e.target.value))} />
      <span className="slider__value">{format ? format(value) : value}</span>
    </label>
  )
}

/* ── NumberField: one number. The label on the left at t-md, then one box of the same
   width everywhere: the value right-aligned in tabular figures, the unit after it inside
   the box. ── */
export function NumberField({ label, value, onChange, min, max, step = 1, unit, disabled, className, hint, onBlur, invalid }: {
  label: ReactNode
  value: number | string | null
  onChange: (text: string) => void
  min?: number
  max?: number
  step?: number
  /** after the field: "tokens", "s" */
  unit?: string
  disabled?: boolean
  className?: string
  hint?: ReactNode
  onBlur?: () => void
  invalid?: boolean
}) {
  return (
    <label className={cx('number-field', className)}>
      <span className="number-field__label">{label}{hint && <span className="field-group__hint">{hint}</span>}</span>
      <span className="field number-field__box">
        <input className="input--num" type="number" inputMode="decimal" min={min} max={max} step={step}
               disabled={disabled} value={value ?? ''} aria-invalid={invalid || undefined}
               onChange={e => onChange(e.target.value)} onBlur={onBlur} />
        {unit && <span className="number-field__unit">{unit}</span>}
      </span>
    </label>
  )
}

/* ── Checkbox: for lists of things to pick ── */
export function Checkbox({ checked, indeterminate = false, onChange, disabled, className, ariaLabel, label }: {
  checked: boolean
  /** the words beside the box; the whole line toggles */
  label?: ReactNode
  indeterminate?: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
  className?: string
  ariaLabel?: string
}) {
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => { if (ref.current) ref.current.indeterminate = indeterminate }, [indeterminate])
  const box = <input ref={ref} type="checkbox" checked={checked} disabled={disabled} aria-label={ariaLabel}
                     onChange={e => onChange(e.target.checked)} />
  return label != null
    ? <label className={cx('checkbox', className)}>{box}<span>{label}</span></label>
    : <span className={cx('checkbox', className)}>{box}</span>
}

/* ── Switch: for settings that are on or off. The whole row is the button. ── */
export function Switch({ checked, onChange, label, disabled, hideLabel = false, className, title }: {
  /** 'mixed' when some of a group are on (a skill with some tools off) */
  checked: boolean | 'mixed'
  onChange: (next: boolean) => void
  label: ReactNode
  disabled?: boolean
  /** the label is still the accessible name, just not drawn */
  hideLabel?: boolean
  className?: string
  title?: string
}) {
  const state = checked === 'mixed' ? 'mixed' : checked ? 'true' : 'false'
  return (
    <button type="button" role={checked === 'mixed' ? 'checkbox' : 'switch'} aria-checked={state}
            disabled={disabled} title={title}
            className={cx('switch-row', hideLabel && 'switch-row--bare', className)}
            onClick={() => onChange(checked !== true)}>
      {/* the track first, so it sits right next to its label however wide the row is */}
      <span className="switch" data-checked={state} aria-hidden />
      {hideLabel ? <span className="sr-only">{label}</span> : (
        <span className="switch-row__text">{label}</span>
      )}
    </button>
  )
}

/* ── InlineEdit: rename in place. Enter or leaving the field saves, Escape cancels;
   the parent shows it instead of the name while editing. ── */
export function InlineEdit({ value, label, placeholder, onSave, onCancel, className }: {
  value: string
  /** accessible name ("Rename …") */
  label: string
  placeholder?: string
  /** the trimmed text ('' when emptied) */
  onSave: (text: string) => void
  onCancel: () => void
  className?: string
}) {
  const done = useRef(false)
  const finish = (fn: () => void) => { if (done.current) return; done.current = true; fn() }
  return (
    <input className={cx('inline-edit', className)} autoFocus defaultValue={value} placeholder={placeholder}
           aria-label={label} spellCheck={false}
           onClick={e => e.stopPropagation()}
           onBlur={e => { const t = e.target.value.trim(); finish(() => onSave(t)) }}
           onKeyDown={e => {
             if (e.key === 'Enter') { e.preventDefault(); const t = e.currentTarget.value.trim(); finish(() => onSave(t)) }
             if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(onCancel) }
           }} />
  )
}

/* ── Form: the fields of one form schema (server/celine/settings/form.py), one column,
   each field its label above its control, a switch its label beside it. Switches and
   choices change at once; numbers, text and lists when you leave the field or press
   Enter. A secret is write-only: "Saved" with Remove once stored (onChange(key, null)),
   else a password field (onChange(key, text)). A copy is read-only with Copy. `config`
   fields are read-only. A field's help is its tooltip. `plain`: a number is laid out like
   every other field (the onboarding's cards), not as the NumberField with its label at
   the left. ── */
export type FormField = {
  key: string
  label: string
  type: 'bool' | 'choice' | 'number' | 'duration' | 'text' | 'list' | 'secret' | 'copy'
  help?: string
  default?: unknown
  choices?: [string, string][]
  unit?: string | null
  min?: number | null
  max?: number | null
  step?: number
  where?: 'state' | 'config'
  editable?: boolean
  /** on the Advanced page (general settings) */
  advanced?: boolean
  /** an Advanced field's skill, by its label: a section of its own */
  group?: string
  /** a text of several lines: a text box */
  long?: boolean
  mono?: boolean
  placeholder?: string
  /** a copy field's text, filled by the server */
  value?: string
}
export type SecretState = { state: 'set' | 'not set'; set_at: string | null }

export function Form({ fields, values, secrets = {}, onChange, disabled = false, plain = false }: {
  fields: FormField[]
  values: Record<string, unknown>
  secrets?: Record<string, SecretState>
  onChange: (key: string, value: unknown) => void
  disabled?: boolean
  plain?: boolean
}) {
  return (
    <div className="form">
      {fields.map(f => <FormRow key={f.key} f={f} value={values[f.key] ?? f.default} secret={secrets[f.key]}
                                onChange={onChange} disabled={disabled} plain={plain} />)}
    </div>
  )
}

function FormRow({ f, value, secret, onChange, disabled, plain }: {
  f: FormField; value: unknown; secret?: SecretState; onChange: (key: string, value: unknown) => void; disabled: boolean; plain: boolean
}) {
  const readOnly = f.where === 'config'
  const help = f.help || undefined
  if (f.type === 'bool') {
    return <Switch label={f.label} title={help} checked={!!value} disabled={disabled || readOnly}
                   onChange={v => onChange(f.key, v)} />
  }
  if (f.type === 'choice') {
    return (
      <Field label={f.label} group>
        <Choice label={f.label} value={String(value ?? '')} disabled={disabled || readOnly} title={help}
                options={(f.choices ?? []).map(([v, l]) => ({ value: v, label: l }))} onChange={v => onChange(f.key, v)} />
      </Field>
    )
  }
  if (f.type === 'copy') {
    return (
      <Field label={f.label}>
        <span className="form__copy">
          <Input value={f.value ?? ''} readOnly mono spellCheck={false} title={help} />
          <Button icon={Copy} title={help} onClick={() => copied(f.value ?? '')}>Copy</Button>
        </span>
      </Field>
    )
  }
  if (f.type === 'secret' && secret?.state === 'set') {
    return (
      <Field label={f.label} group>
        <span className="form__control">
          <span className="form__saved" title={help}>Saved</span>
          <Button variant="ghost" size="sm" disabled={disabled || readOnly} onClick={() => onChange(f.key, null)}>Remove</Button>
        </span>
      </Field>
    )
  }
  return <TextRow f={f} value={value} help={help} disabled={disabled || readOnly} plain={plain} onChange={onChange} />
}

const shown = (f: FormField, value: unknown) =>
  value == null ? '' : f.type === 'list' && Array.isArray(value) ? value.join('\n') : String(value)

function TextRow({ f, value, help, disabled, plain, onChange }: {
  f: FormField; value: unknown; help?: string; disabled: boolean; plain: boolean; onChange: (key: string, value: unknown) => void
}) {
  const numeric = f.type === 'number' || f.type === 'duration'
  const text = shown(f, value)
  const [draft, setDraft] = useState(text)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { setDraft(text) }, [text])
  const commit = () => {
    if (draft === text) return
    if (numeric) {
      const n = Number(draft)
      if (!draft.trim() || !Number.isFinite(n)) { setError('Enter a number.'); return }
      if (f.min != null && n < f.min) { setError(`At least ${num(f.min)}${f.unit ? ` ${f.unit}` : ''}.`); return }
      if (f.max != null && n > f.max) { setError(`At most ${num(f.max)}${f.unit ? ` ${f.unit}` : ''}.`); return }
      setError(null); onChange(f.key, n)
    } else if (f.type === 'list') {
      setError(null); onChange(f.key, draft.split('\n').map(l => l.trim()).filter(Boolean))
    } else if (f.type === 'secret') {
      if (draft.trim()) onChange(f.key, draft.trim())
    } else { setError(null); onChange(f.key, draft) }
  }
  // a number: the one number field, its label on the left
  if (numeric && !plain) {
    return (
      <div>
        <NumberField label={f.label} value={draft} disabled={disabled} unit={f.unit ?? undefined} step={f.step} invalid={!!error}
                     onChange={setDraft} onBlur={commit} />
        {error && <InlineError compact message={error} />}
      </div>
    )
  }
  if ((f.long && !numeric) || f.type === 'list') {
    return (
      <Field label={f.label} error={error}>
        <TextArea rows={3} value={draft} disabled={disabled} title={help} mono={f.mono} placeholder={f.placeholder}
                  onChange={e => setDraft(e.target.value)} onBlur={commit} />
      </Field>
    )
  }
  const secret = f.type === 'secret'
  return (
    <Field label={f.label} error={error}>
      <span className="form__control">
        <Input value={draft} disabled={disabled} title={help} invalid={!!error} mono={f.mono} placeholder={f.placeholder}
               {...(secret ? { type: 'password', autoComplete: 'off', spellCheck: false } : {})}
               onChange={e => setDraft(e.target.value)} onBlur={commit}
               onKeyDown={e => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') setDraft(text) }} />
        {f.unit && <span className="form__unit">{f.unit}</span>}
      </span>
    </Field>
  )
}
