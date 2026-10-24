/* Slash commands for Celine's composer. The server owns them (server/celine/api/
   commands.py): GET /api/slash-commands lists
   them, POST /api/conversations/{cid}/slash-commands/{name}/run {text} runs one. The set is the
   user's: /compact, /think (also /effort), /model, /usage. A message that starts with
   "/" and names one runs it instead of being sent. "//" at the start sends a literal
   slash. */
import { json, request } from './api'
import { useResource } from './useResource'

type CommandArg = {
  name: string; description: string; required: boolean; choices: string[]; kind: 'choice' | 'text'
  /** words to offer (for /think: off and the current model's effort words) */
  suggestions?: string[]
}
type Command = {
  name: string
  aliases: string[]
  /** "/think [level]" */
  usage: string
  description: string
  /** "idle": refused while Celine is answering */
  when: string
  args: CommandArg[]
}

/** what running one returns: always a sentence in `message` */
type CommandResult = { command: string; ok: boolean; state?: string; message?: string; models?: string[]; [k: string]: unknown }

export const commandsApi = {
  list: (cid: string | null) =>
    request<{ commands: Command[] }>(`/slash-commands${cid ? `?conversation=${encodeURIComponent(cid)}` : ''}`),
  run: (cid: string, name: string, text: string) =>
    request<CommandResult>(`/conversations/${encodeURIComponent(cid)}/slash-commands/${encodeURIComponent(name)}/run`,
      json('POST', { text })),
}

/** the server's commands; `version` changes when the conversation's model changes */
export function useCommands(cid: string | null, version: unknown): Command[] {
  const res = useResource(() => commandsApi.list(cid).then(r => r.commands), [cid, version])
  return res.data ?? []
}

const names = (c: Command) => [c.name, ...c.aliases]

/** one row of the menu above the composer: picking it puts `fill` in the box */
type MenuRow = { key: string; label: string; fill: string }

/** While the name is typed: the commands that match. After "/think ": its suggested
    words; after "/model ": the models (`models`, loaded by the caller). */
export function menuRows(text: string, commands: Command[], models: string[] | null): MenuRow[] {
  const name = /^\/([\w-]*)$/.exec(text)
  if (name) {
    const typed = name[1]!.toLowerCase()
    return commands.filter(c => names(c).some(n => n.startsWith(typed)))
      .sort((a, b) => a.name.localeCompare(b.name))
      .map(c => ({
        key: c.name,
        label: c.usage + (c.aliases.length ? `  (also ${c.aliases.map(a => `/${a}`).join(', ')})` : ''),
        fill: `/${c.name} `,
      }))
  }
  const arg = /^\/([\w-]+)\s+(\S*)$/.exec(text)
  if (!arg) return []
  const typed = arg[1]!.toLowerCase()
  const cmd = commands.find(c => names(c).includes(typed))
  if (!cmd) return []
  const word = arg[2]!.toLowerCase()
  const words = cmd.name === 'model' ? models ?? [] : cmd.args[0]?.suggestions ?? []
  return words.filter(w => w.toLowerCase().includes(word) && w.toLowerCase() !== word)
    .slice(0, 50)
    .map(w => ({ key: w, label: w, fill: `/${typed} ${w}` }))
}

/** "/name args" → the command and its args, when the server has it */
export function parseCommand(text: string, commands: Command[]): { command: Command; args: string } | null {
  const t = text.trim()
  if (t.startsWith('//')) return null
  const m = /^\/([\w-]+)(?:\s+([\s\S]*))?$/.exec(t)
  if (!m) return null
  const typed = m[1]!.toLowerCase()
  const command = commands.find(c => names(c).includes(typed))
  return command ? { command, args: (m[2] ?? '').trim() } : null
}

/** the server's rule for a thinking word: one word of letters, digits, _ . -, at most 24 */
export const THINK_WORD = /^[A-Za-z0-9_.-]{1,24}$/
