/* The agent's face (the kit's Face): its portrait (its url from the status changes with every new
   one), else its initial in the serif on its colour. The agent's card offers Open Settings, or the
   caller's actions. */
import { Settings2 } from 'lucide-react'
import { colorOf, useHer } from '../lib/her'
import { useNav } from '../lib/nav'
import { Face, type FaceAction, type FaceSize } from '../ui'

export function Portrait({ size = 'md', className, speaking = false, away = false, actions }: {
  size?: FaceSize; className?: string
  /** it is writing now: its ring pulses */
  speaking?: boolean
  /** its turn waits behind another */
  away?: boolean
  /** its card's actions; left out: Open settings */
  actions?: FaceAction[]
}) {
  const her = useHer()
  const nav = useNav()
  const color = colorOf(her)
  const open: FaceAction[] = [{ label: 'Open Settings', icon: Settings2, onSelect: () => nav.go({ section: 'settings', page: 'agent' }) }]
  return <Face name={her?.name ?? ''} src={her?.portrait?.url} size={size} className={className} speaking={speaking} away={away}
               bg={`color-mix(in oklch, ${color} 22%, var(--surface-2))`} fg={color}
               info={{ line: her?.name ?? '', actions: actions ?? open }} />
}
