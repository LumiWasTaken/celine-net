/* The agent's sub-agents across every conversation: GET /api/sub-agents once, then each
   `agent.state` on the feed replaces its row, and `changes` (after a reconnect) brings
   them all again. Stop is POST /api/sub-agents/{id}/stop. */
import { createContext, useState } from 'react'
import { post, request, type Crumb, type Home } from './api'
import { useServerEvents } from './bus'
import type { Said } from './states'
import { useResource } from './useResource'

export type Agent = {
  id: string
  /** the conversation that started it */
  parent: string
  name: string
  host: string
  access: 'read' | 'work'
  folder: string | null
  /** at work (its last turn has not reported) */
  working: boolean
  state: TreeAgent['state']
  /** that state as the pages say it, the server's */
  words: string
  tone: Said['tone']
  /** the tool its live turn runs, null between steps */
  step: string | null
  /** that step in words, the server's ("Reading the note") */
  step_words: string | null
  /** its latest report */
  report?: { id: number; text: string } | null
  created_at: string
  updated_at: string
  /** where it lives (its root's home) and the path that opens it, the server's */
  home: Home
  link: string
}

/** one sub-agent in the tree under a conversation's top (GET /api/conversations/{id}/sub-agents,
    server/celine/sub_agents/skill.py tree_of) */
export type TreeAgent = {
  id: string
  parent: string
  /** 1: started by the top conversation */
  depth: number
  name: string
  state: 'queued' | 'working' | 'done' | 'stopped' | 'failed' | 'paused'
  /** that state as the pages say it, the server's */
  words: string
  tone: Said['tone']
  /** why it failed, stopped or paused, the server's words */
  reason: string | null
  step_words: string | null
  model: string | null
  prompt_tokens: number
  completion_tokens: number
  seconds: number
  link: string
}

/** a mission the top or one of its sub-agents started, in the same tree: its status,
    and its note as the reason while it waits for you or stopped */
export type TreeMission = Omit<TreeAgent, 'state' | 'step_words'> & {
  state: 'active' | 'needs_you' | 'done' | 'stopped'
}

export const agentsApi = {
  list: () => request<{ agents: Agent[] }>('/sub-agents').then(r => r.agents),
  /** `origin`: where the top came from (the run that started a mission); `total`: what the
      top and everything under it took */
  tree: (cid: string) => request<{
    top: string; origin: Crumb[]; agents: TreeAgent[]; missions: TreeMission[]
    total: { prompt_tokens: number; completion_tokens: number; seconds: number }
    states: Record<TreeAgent['state'] | TreeMission['state'], Said>
  }>(`/conversations/${encodeURIComponent(cid)}/sub-agents`),
  stop: (id: string) => post<{ stopped: boolean }>(`/sub-agents/${encodeURIComponent(id)}/stop`),
}

export function useAgents(): Agent[] {
  const res = useResource(() => agentsApi.list(), [])
  const [live, setLive] = useState<Map<string, Agent>>(new Map())
  useServerEvents(ev => {
    if (ev.type === 'agent.state' && typeof ev.id === 'string') {
      const a = ev as unknown as Agent
      setLive(m => new Map(m).set(a.id, a))
    }
    if (ev.type === 'changes' && Array.isArray(ev.agents)) {
      setLive(new Map())
      res.set(ev.agents as Agent[])
    }
    if (ev.type === 'open') res.reload()
  })
  const base = res.data ?? []
  const merged = base.map(a => live.get(a.id) ?? a)
  for (const a of live.values()) if (!base.some(b => b.id === a.id)) merged.unshift(a)
  return merged
}

/** the sub-agents as useAgents has them, for the tool rows that started them */
export const AgentsContext = createContext<Agent[]>([])
