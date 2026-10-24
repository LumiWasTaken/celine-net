/* Settings, Appearance: Colour (Accent colour, Tint, Contrast), Background (Grain, its
   Opacity), Chat (Text size, Hide thinking, Hide tool calls, Collapse thinking by default),
   and beside them a live preview drawn by the real components on the real canvas: the
   conversation list, your message, its reply with its thinking and a tool row, the
   composer. A choice shows at once on the whole app (lib/appearance.ts previewAppearance)
   and saves itself: a switch or a choice at once, a slider when it rests (lib/useAutosave).
   Reset to defaults puts every key back to the server's default. The choices scroll in
   the page; on a wide screen the preview stays beside them, on a phone it follows them. */
import { useEffect, useRef, useState } from 'react'
import { Activity, Brain, Copy, Eye, Plus, RefreshCw } from 'lucide-react'
import { ACCENTS, CONTRAST_MAX, GRAINS, previewAppearance, saveAppearance, useAppearance, type Appearance as Look } from '../../lib/appearance'
import { errorText } from '../../lib/format'
import { useHer } from '../../lib/her'
import { capital, usePronouns } from '../../lib/name'
import { OFFLINE_TITLE, useOnline } from '../../lib/socket'
import { useAutosave } from '../../lib/useAutosave'
import {
  Button, Choice, ComposerBox, ConfirmDialog, Face, Field, Fold, IconButton, InlineError, ListRow, Message, Section, Segmented, Slider, Switch, Thoughts,
  type Option,
} from '../../ui'
import { ToolLines } from '../../components/ToolRow'
import type { ToolCall } from '../../lib/api'
import { Page } from './parts'
import '../../components/Transcript.css'

const NAME: Record<string, string> = { rose: 'Rose', peach: 'Peach', sage: 'Sage', teal: 'Teal', sky: 'Sky', violet: 'Violet', miku: 'Miku' }

/** a colour as a swatch with its name (macOS "Accent color"): the kit's Choice tiles */
const swatch = <V extends string>(v: V) => ({ value: v, label: NAME[v]!, swatch: `var(--char-${v})` })

const GRAIN_WORD: Record<Look['grain'], string> = { fine: 'Fine', coarse: 'Coarse', paper: 'Paper', none: 'None' }
const GRAIN_OPTIONS: Option<Look['grain']>[] = GRAINS.map(g => ({ value: g, label: GRAIN_WORD[g] }))
const pct = (v: number) => `${v}%`

const SIZES: Option<Look['text_size']>[] = [
  { value: 'sm', label: 'Small' }, { value: 'md', label: 'Default' }, { value: 'lg', label: 'Large' }, { value: 'xl', label: 'Larger' },
]

export function Appearance() {
  const online = useOnline()
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const auto = useAutosave(async change => {
    try { await saveAppearance(change as Partial<Look>) } catch (e) {
      // left with a failed save: the app shows the saved look again
      if (!alive.current) previewAppearance({})
      throw e
    }
  })
  const pending = auto.pending as Partial<Look>
  const look: Look = { ...useAppearance(), ...pending }
  useEffect(() => { previewAppearance(pending) }, [pending])
  const [resetting, setResetting] = useState(false)
  const put = <K extends keyof Look>(k: K, when: 'now' | 'soon' = 'now') => (v: Look[K]) => auto.set(k, v, when)
  return (
    <Page title="Appearance" wide
          actions={<Button disabled={!online} title={online ? undefined : OFFLINE_TITLE} onClick={() => setResetting(true)}>Reset to defaults</Button>}>
      <div className="appearance">
        <div className="appearance__choices">
        <Section title="Colour">
          <Field group label="Accent colour">
            <Choice label="Accent colour" value={look.accent} onChange={put('accent')} options={ACCENTS.map(swatch)} />
          </Field>
          <Slider label={<span title="How much the accent colours the background and surfaces">Tint</span>}
                  value={look.tint} onChange={put('tint', 'soon')} min={0} max={300} step={25} format={pct} />
          <Slider label={<span title="The step in lightness between the background and surfaces">Contrast</span>}
                  value={look.contrast} onChange={put('contrast', 'soon')} min={50} max={CONTRAST_MAX} step={10} format={pct} />
        </Section>
        <Section title="Background">
          <Field group label="Grain">
            <Segmented label="Grain" value={look.grain} onChange={put('grain')} options={GRAIN_OPTIONS} />
          </Field>
          <Slider label="Opacity" value={look.grain_opacity} onChange={put('grain_opacity', 'soon')} min={0} max={20} step={1} format={pct}
                  disabled={look.grain === 'none'} />
        </Section>
        <Section title="Chat">
          <Field group label="Text size">
            <Segmented label="Text size" value={look.text_size} onChange={put('text_size')} options={SIZES} />
          </Field>
          <Switch label="Hide thinking" checked={look.hide_thinking} onChange={put('hide_thinking')} />
          <Switch label="Hide tool calls" checked={look.hide_tools} onChange={put('hide_tools')} />
          <Switch label="Collapse thinking by default" checked={look.collapse_thinking} onChange={put('collapse_thinking')}
                  disabled={look.hide_thinking} />
          {auto.error && <InlineError compact message={auto.error} onRetry={auto.flush} />}
        </Section>
        </div>
        <Preview hideThinking={look.hide_thinking} hideTools={look.hide_tools} collapseThinking={look.collapse_thinking} />
      </div>
      {resetting && (
        <ConfirmDialog title="Reset the appearance?" confirmLabel="Reset to defaults" onClose={() => setResetting(false)}
                       onConfirm={async () => {
                         auto.clear()
                         try { await saveAppearance(Object.fromEntries(Object.keys(look).map(k => [k, null]))); setResetting(false) }
                         catch (e) { return errorText(e) }
                       }} />
      )}
    </Page>
  )
}

const ASKED = 'what was that noise in the hall?'
const THOUGHTS = 'Sam asked about the noise. Maya next door moved a bookshelf this morning, and a box of books fell over. Keep it short and kind.'
const REPLY = "maya next door is moving a bookshelf. a box of books fell over. nothing broke, she's fine."
const SAMPLE_CALL: ToolCall = {
  id: 'preview-call',
  name: 'user_documents__read',
  arguments: '{"document":"house-inventory.md"}',
  state: 'ok',
  display: { title: 'house-inventory.md' },
  words: { running: 'Reading "house-inventory.md"', settled: 'Read "house-inventory.md"', step: 'Reading a document', where: null },
  started_at: '2026-01-19T10:00:00Z',
  finished_at: '2026-01-19T10:00:01Z',
}

/** one chat reply, in the real type, colours and sizes; a picture of the look, so nothing
    in it acts (its action row shown as on a hover) */
function Preview({ hideThinking, hideTools, collapseThinking }: { hideThinking: boolean; hideTools: boolean; collapseThinking: boolean }) {
  const her = useHer()
  const name = her?.name ?? ''
  const words = usePronouns()
  const thoughtsTitle = `${capital(words.possessive)} thoughts`
  const [draft, setDraft] = useState('what did she knock over?')
  const face = (name: string, hue: string) => (
    <Face name={name} size="sm" bg={`color-mix(in oklch, var(--char-${hue}) 22%, var(--surface-2))`} fg={`var(--char-${hue})`} still />
  )
  return (
    <div className="appearance__preview" inert>
      <div className="appearance__list">
        <ListRow density="sm" title="Moving day" selected />
        <ListRow density="sm" title="Sunday plans" />
        <ListRow density="sm" title="Book club" />
      </div>
      <div className="appearance__chat">
        <Message mine>{ASKED}</Message>
        <Message face={face(name, her?.color ?? 'rose')} shown
                 actions={<><IconButton size="sm" icon={RefreshCw} label="Regenerate" /><IconButton size="sm" icon={Copy} label="Copy" /></>}
                 menu={[
                   ...(hideThinking ? [{ label: thoughtsTitle, icon: Brain, onSelect: () => {} }] : []),
                   { label: 'Activity', icon: Activity, onSelect: () => {} }, { label: `What ${name || words.subject} saw`, icon: Eye, onSelect: () => {} },
                 ]}>
          <div className="reply">
            {!hideThinking && (
              <Fold key={String(collapseThinking)} title={thoughtsTitle} icon={Brain} defaultOpen={!collapseThinking}>
                <Thoughts>{THOUGHTS}</Thoughts>
              </Fold>
            )}
            <div className="msg__prose">{REPLY}</div>
            <ToolLines calls={[SAMPLE_CALL]} inline={!hideTools} />
          </div>
        </Message>
        <ComposerBox value={draft} onChange={setDraft} onSend={() => setDraft('')} placeholder={`Message ${name}`}
                     start={<IconButton icon={Plus} label="More" />} />
      </div>
    </div>
  )
}
