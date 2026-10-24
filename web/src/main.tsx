import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/tokens.css'
import './styles/base.css'
// the kit before the views' own CSS, so a view can adjust a kit class
import './ui/ui.css'
import { App } from './App'

// the page is the visible part of the window: iOS ignores interactive-widget and lays the
// keyboard over the page, so the height follows the visual viewport (pinch zoom left alone).
// An on-screen keyboard is up while the visible part is clearly shorter than the tallest it
// has been at this width (Android shrinks the window with it, so the window says nothing):
// only then the phone's bottom bar steps aside (App.css data-keyboard)
const vv = window.visualViewport
if (vv) {
  let wide = 0, tall = 0
  const touch = matchMedia('(pointer: coarse)')
  const fit = () => {
    if (Math.abs(vv.scale - 1) > 0.01) return
    const root = document.documentElement
    if (window.innerWidth !== wide) { wide = window.innerWidth; tall = 0 }
    tall = Math.max(tall, vv.height, window.innerHeight)
    root.style.setProperty('--vh', `${Math.round(vv.height)}px`)
    if (touch.matches && tall - vv.height > 120) root.dataset.keyboard = ''
    else delete root.dataset.keyboard
    if (window.scrollY) window.scrollTo(0, 0)
  }
  vv.addEventListener('resize', fit)
  vv.addEventListener('scroll', fit)
  fit()
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
