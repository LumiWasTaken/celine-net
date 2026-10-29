/* The service worker (lib/notify.ts registers it at the root scope): the server's pushes
   (server/celine/push.py, {title, body, link, tag}) and the page's own notices as system
   notifications. A click focuses an open page of the app and opens the link there (the page
   hears `{open: link}`), or opens a new window at the link. No fetch handler: nothing is
   cached, every request goes to the network as without it. */
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()))

self.addEventListener('push', e => {
  const n = e.data ? e.data.json() : {}
  e.waitUntil(self.registration.showNotification(n.title || 'Celine', {
    body: n.body, tag: n.tag, icon: '/icon-192.png', data: { link: n.link },
  }))
})

self.addEventListener('notificationclick', e => {
  e.notification.close()
  const link = (e.notification.data && e.notification.data.link) || '/'
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(pages => {
    const page = pages.find(p => p.focused) || pages[0]
    if (!page) return self.clients.openWindow(link)
    page.postMessage({ open: link })
    return page.focus()
  }))
})
