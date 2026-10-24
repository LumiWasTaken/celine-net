/* A picture at full size (the kit's Viewer): it grows from the picture you clicked, zooms,
   pans and flips through a gallery (Left and Right). Titled by what it shows (one Celine
   made: its prompt's first phrase), never by its file name; its prompt shown whole under
   it; how it was made (preset, size and seed) in "..." under Copy prompt
   . Its menu also has Use as portrait: the picture becomes its own. */
import { useState } from 'react'
import { Copy, UserRound } from 'lucide-react'
import { api } from '../lib/api'
import { errorText } from '../lib/format'
import { titleOf } from '../lib/pictures'
import { InlineError, Viewer, type MenuItem, copied, toast } from '../ui'
import './PhotoViewer.css'

export function PhotoViewer({ src, name, prompt, details, onClose, onPrev, onNext, index, total }: {
  src: string
  /** what it shows, when there is no prompt to say it */
  name: string
  prompt?: string | null
  /** how it was made: "anima-turbo, 832 x 1216, seed 1234" */
  details?: string | null
  onClose: () => void
  onPrev?: () => void
  onNext?: () => void
  index?: number
  total?: number
}) {
  const [error, setError] = useState<string | null>(null)
  const portrait = () => { setError(null); api.setPortrait(src).then(() => toast({ text: 'Portrait set' })).catch(e => setError(errorText(e))) }
  return (
    <Viewer src={src} name={prompt ? titleOf(prompt) : name} more={madeMenu({ prompt, details, onPortrait: portrait })}
            caption={<>{prompt && <p className="photo-viewer__prompt">{prompt}</p>}{error && <InlineError compact message={error} />}</>}
            onClose={onClose} onPrev={onPrev} onNext={onNext} index={index} total={total} />
  )
}

/** a made picture's own menu items: Copy prompt with how it was made under it, Use as portrait */
export function madeMenu({ prompt, details, onPortrait }: {
  prompt?: string | null; details?: string | null; onPortrait: () => void
}): MenuItem[] {
  return [
    ...(prompt ? [{ label: 'Copy prompt', icon: Copy, detail: details ?? undefined,
                    onSelect: () => copied(prompt) }] : []),
    { label: 'Use as portrait', icon: UserRound, onSelect: onPortrait },
  ]
}
