/* References resolved in batches: messages ([[whatsapp:123]]) and document quotes
   ([[documents:812]]). Everything that renders a reference in the same frame shares one
   /api/archive/refs request; results are cached for the session. */
import { useEffect, useState } from 'react'
import { refsApi, type DocumentLink, type RefCard } from './kb'

/** the refs asked within this long go out as one request */
const BATCH_MS = 30

type Entry = { state: 'loading' } | { state: 'ok'; card: RefCard } | { state: 'missing' } | { state: 'error'; error: string }

const cache = new Map<string, Entry>()
const waiting = new Map<string, Set<() => void>>()
let queue = new Set<string>()
let timer: number | undefined

function notify(ref: string) {
  for (const fn of waiting.get(ref) ?? []) fn()
}

function flush() {
  timer = undefined
  const batch = [...queue]
  queue = new Set()
  if (!batch.length) return
  for (let i = 0; i < batch.length; i += 100) {
    const chunk = batch.slice(i, i + 100)
    refsApi.resolve(chunk).then(res => {
      for (const r of chunk) {
        const card = res.refs[r]
        cache.set(r, card ? { state: 'ok', card } : { state: 'missing' })
        notify(r)
      }
    }).catch((e: unknown) => {
      for (const r of chunk) {
        cache.set(r, { state: 'error', error: e instanceof Error ? e.message : String(e) })
        notify(r)
      }
    })
  }
}

function want(ref: string) {
  if (cache.has(ref)) return
  cache.set(ref, { state: 'loading' })
  queue.add(ref)
  if (timer === undefined) timer = window.setTimeout(flush, BATCH_MS)
}

export function retryRef(ref: string) {
  cache.delete(ref)
  want(ref)
  notify(ref)
}

export function useRefEntry(ref: string): Entry {
  const [, bump] = useState(0)
  useEffect(() => {
    const fn = () => bump(n => n + 1)
    let set = waiting.get(ref)
    if (!set) { set = new Set(); waiting.set(ref, set) }
    set.add(fn)
    want(ref)
    return () => { set!.delete(fn) }
  }, [ref])
  return cache.get(ref) ?? { state: 'loading' }
}

/** a document by its name ([[Some file]] in a note) → its id, or null */
export async function findDocument(name: string): Promise<DocumentLink | null> {
  const res = await refsApi.resolve([name])
  return res.items?.[name] ?? null
}
