/* The agent's face (the kit's Face): its portrait (its url from the status changes with every new
   one), else its initial in the serif on its colour. */
import { colorOf, useAgent } from '../lib/agent'
import { Face, type FaceSize } from '../ui'

export function Portrait({ size = 'md', className, speaking = false, away = false }: {
  size?: FaceSize; className?: string
  /** it is writing now: its ring pulses */
  speaking?: boolean
  /** its turn waits behind another */
  away?: boolean
}) {
  const agent = useAgent()
  const color = colorOf(agent)
  return <Face name={agent?.name ?? ''} src={agent?.portrait?.url} size={size} className={className} speaking={speaking} away={away}
               bg={`color-mix(in oklch, ${color} 22%, var(--surface-2))`} fg={color} />
}
