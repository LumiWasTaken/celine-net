/* Keys. Enter sends in every composer: the kit's isSendKey (ui/Message.tsx). */

/** the focus is in something you type into */
export const isTyping = (t: EventTarget | null) =>
  t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))

/** a form with long text saves or starts on Ctrl+Enter (and Ctrl+S, which never opens the
    browser's own save) */
export const isSaveKey = (e: { key: string; ctrlKey: boolean; metaKey: boolean }) =>
  (e.ctrlKey || e.metaKey) && (e.key === 'Enter' || e.key.toLowerCase() === 's')
