/* First run: one line under its name and in a conversation, "Choose a model" as a link into
   Settings, Models, while the Chat role has no model. Nothing renders once it has one, or
   while it loads. The agent's name, yours and the features are the onboarding's (views/Welcome.tsx). */
import { ArrowRight } from 'lucide-react'
import { format } from '../../lib/nav'
import { SETTINGS_EVENTS, settingsApi } from '../../lib/settingsApi'
import { useResource } from '../../lib/useResource'
import { Button } from '../../ui'
import './FirstRun.css'

export function FirstRun() {
  const roles = useResource(() => settingsApi.roles(), [], { events: SETTINGS_EVENTS })
  if (!roles.data || (roles.data.roles.chat?.chain.length ?? 0) > 0) return null
  return (
    <div className="first-run">
      <Button variant="ghost" iconEnd={ArrowRight} href={format({ section: 'settings', page: 'models' })}>Choose a model</Button>
    </div>
  )
}
