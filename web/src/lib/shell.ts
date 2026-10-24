/* What the app's frame does on every page (App.tsx Shell): the window's title, the keys of
   the whole app, and where the focus goes after a press while a field had it. */
import { useEffect } from 'react'
import { emit } from './bus'
import { isTyping } from './keys'
import type { Nav } from './nav'

/** the window's title: the waiting count first (`count`, 0 for none), then `where` */
export function useWindowTitle(where: string, count: number) {
  useEffect(() => { document.title = `${count ? `(${count}) ` : ''}${where}` }, [where, count])
}

/** Ctrl+K: Go to; Ctrl+Shift+O: a new chat; ?: the shortcuts */
export function useGlobalKeys(nav: Nav, on: { goTo: () => void; keys: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey
      if (mod && !e.shiftKey && e.key.toLowerCase() === 'k') { e.preventDefault(); on.goTo() }
      else if (mod && e.shiftKey && e.key.toLowerCase() === 'o') {
        e.preventDefault()
        nav.go({ section: 'celine' })
        emit({ type: 'ui.new' })
      } else if (e.key === '?' && !mod && !isTyping(e.target)) { e.preventDefault(); on.keys() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [nav]) // eslint-disable-line react-hooks/exhaustive-deps
}

/* A press on a button while a field has the focus: the focus leaving the field could move
   the page under the pointer before the click lands (the phone's bar coming back, a list
   closing). So the focus stays in the field until the click has landed, then goes to the
   button; a [data-keep-focus] button (Send) leaves it in the field, so a keyboard stays
   up. Any pointer; a press on text (to select it) is left alone. */
export function useFocusHandback() {
  useEffect(() => {
    let tapped: Element | null = null, field: Element | null = null
    const down = (e: PointerEvent) => {
      if (!isTyping(document.activeElement) || isTyping(e.target)) return
      if (!(e.target instanceof Element) || !e.target.closest('button, a[href], [role="button"], [role="tab"], [role="radio"], [tabindex]:not([tabindex="-1"])')) return
      e.preventDefault()
      tapped = e.target
      field = document.activeElement
    }
    const click = () => {
      const t = tapped
      tapped = null
      if (!t) return
      setTimeout(() => {
        // the click moved the focus itself (a menu it opened took it): leave it there
        if (document.activeElement !== field) return
        if (t.closest('[data-keep-focus]')) return
        const to = t.isConnected ? t.closest<HTMLElement>('button, a[href], [tabindex]') : null
        if (to) to.focus({ preventScroll: true })
        else if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
      })
    }
    document.addEventListener('pointerdown', down, true)
    document.addEventListener('click', click, true)
    return () => {
      document.removeEventListener('pointerdown', down, true)
      document.removeEventListener('click', click, true)
    }
  }, [])
}
