/* Before the first paint: the look this device saw last (lib/appearance.ts keeps it under
   "look"), so the canvas never flashes another colour. The server's look follows. */
try {
  var a = JSON.parse(localStorage.getItem('look') || 'null')
  if (a) {
    var h = document.documentElement
    h.dataset.accent = a.accent
    h.dataset.textSize = a.text_size
    h.dataset.grain = a.grain
    h.style.setProperty('--grain-opacity', String(a.grain_opacity / 100))
    h.style.setProperty('--tint', String(a.tint / 100))
    h.style.setProperty('--contrast', String(a.contrast / 100))
    h.style.setProperty('--boot-canvas', a.canvas)
    document.querySelector('meta[name="theme-color"]').setAttribute('content', a.canvas)
  }
} catch (e) { /* storage blocked: the default look until the server answers */ }
