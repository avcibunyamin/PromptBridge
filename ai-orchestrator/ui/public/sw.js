// TB-12: the phone's offline shell only. Hashed /assets/ files are cache-first; the panel page is network-first
// with the cached copy as fallback. API, events and health are never touched (no respondWith): no stale or
// cached private data, the device token is only ever sent to the network.
const CACHE = 'conductor-shell-v1'
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())))
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url)
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return
  if (url.pathname.startsWith('/assets/')) {
    e.respondWith(caches.open(CACHE).then(c => c.match(e.request).then(hit => hit || fetch(e.request).then(r => { if (r.ok) c.put(e.request, r.clone()); return r }))))
  } else if (e.request.mode === 'navigate' && url.pathname.startsWith('/ui')) {
    // clone now: once the page reads the body, a later clone() throws
    e.respondWith(fetch(e.request).then(r => { if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put('/ui/', copy)) } return r })
      .catch(() => caches.match('/ui/')))
  }
})

// TB-29 (K-036): a notice from the Mac (end-to-end encrypted Web Push); a tap opens that work in the app.
self.addEventListener('push', e => {
  let d = {}
  try { d = e.data ? e.data.json() : {} } catch { d = { body: e.data ? e.data.text() : '' } }
  e.waitUntil(self.registration.showNotification(d.title || 'PromptBridge',
    { body: d.body || '', icon: '/ui/icon-192.png', tag: d.tag, renotify: !!d.tag, data: { url: d.url || '/ui/' } }))
})
self.addEventListener('notificationclick', e => {
  e.notification.close()
  const url = new URL((e.notification.data && e.notification.data.url) || '/ui/', self.location.origin)
  if (url.origin !== self.location.origin || !url.pathname.startsWith('/ui')) return   // only this app's own pages
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    const open = list.find(c => c.url.startsWith(self.location.origin + '/ui'))
    return open ? open.navigate(url.href).then(c => (c || open).focus()) : self.clients.openWindow(url.href)
  }))
})
