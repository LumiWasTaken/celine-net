/* The window's title has one writer, App: the waiting count first, then the page. A page
   with a name of its own (a terminal, an artifact) hands it over here. */
import { useSyncExternalStore } from 'react'

let page = ''
const subs = new Set<() => void>()

export function setPageName(name: string) {
  if (page === name) return
  page = name
  subs.forEach(f => f())
}

export const usePageName = () =>
  useSyncExternalStore(f => { subs.add(f); return () => { subs.delete(f) } }, () => page)
