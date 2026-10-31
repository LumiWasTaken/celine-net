"""The login screen: one self-contained page (styles, script and the Geist font
inline), because nothing else is served before a login. Its `:root` copies the tokens
it uses from web/src/styles/tokens.css, value for value (tests/test_auth.py checks),
since the image ships the built UI and not its sources."""
from __future__ import annotations

import base64
import functools
import hashlib
import json
import re
from pathlib import Path

from starlette.responses import HTMLResponse


@functools.cache
def _font() -> str:
    raw = (Path(__file__).parent / "geist-latin.woff2").read_bytes()
    return base64.b64encode(raw).decode()


def login_page(setup: bool, methods: list[str], origin: str) -> HTMLResponse:
    """`setup`: nothing can log in yet, the page asks the setup code and offers the
    three methods. `methods`: the kinds that exist, the login view shows each."""
    html = (_PAGE.replace("__FONT__", _font())
            .replace("__SETUP__", "true" if setup else "false")
            .replace("__METHODS__", json.dumps(methods))
            .replace("__ORIGIN__", json.dumps(origin)))
    return HTMLResponse(html, headers={"cache-control": "no-store",
                                       "content-security-policy": _csp(html),
                                       "x-frame-options": "DENY",
                                       "referrer-policy": "no-referrer"})


def _csp(html: str) -> str:
    """Only this page's own inline script may run (its hash), and nothing may frame it."""
    script = re.search(r"<script>(.*?)</script>", html, re.S)[1]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    return (f"default-src 'none'; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; "
            f"font-src data:; img-src data:; connect-src 'self'; base-uri 'none'; "
            f"form-action 'none'; frame-ancestors 'none'")


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Celine - Login</title>
<style>
@font-face{font-family:'Geist Variable';font-style:normal;font-display:swap;font-weight:100 900;
  src:url(data:font/woff2;base64,__FONT__) format('woff2')}
:root{
  --font-sans:  'Geist Variable', ui-sans-serif, system-ui, -apple-system, sans-serif;
  --font-mono:  'JetBrains Mono Variable', ui-monospace, 'SF Mono', Menlo, monospace;
  --text-scale: 1;
  --t-xs:  calc(clamp(0.78125rem, 0.7582rem + 0.0974vw, 0.875rem) * var(--text-scale));
  --t-md:  calc(clamp(0.875rem, 0.8442rem + 0.1299vw, 1rem) * var(--text-scale));
  --t-xl:  calc(clamp(1.25rem, 1.2115rem + 0.1623vw, 1.40625rem) * var(--text-scale));
  --lh-ui:    1.4;
  --lh-body:  1.5;
  --fw-regular: 450;
  --fw-medium:  580;
  --fw-bold:    700;
  --s-1: 4px;  --s-2: 8px;  --s-3: 12px; --s-4: 16px;
  --s-5: 24px; --s-6: 32px; --s-7: 48px;
  --h-md: 40px;
  --h-lg: 44px;
  --dialog-sm: 440px;
  --r-md: 10px;
  --r-lg: 16px;
  --r-full: 999px;
  --dur-fast: 120ms;
  --tint: 1; --contrast: 1;
  --bg-canvas:     oklch(from var(--accent) 0.161 calc(0.012 * var(--tint)) h);
  --surface-1:     oklch(from var(--accent) calc(0.161 + 0.034 * var(--contrast)) calc(0.015 * var(--tint)) h);
  --surface-2:     oklch(from var(--accent) calc(0.161 + 0.075 * var(--contrast)) calc(0.016 * var(--tint)) h);
  --surface-3:     oklch(from var(--accent) calc(0.161 + 0.114 * var(--contrast)) calc(0.018 * var(--tint)) h);
  --surface-4:     oklch(from var(--accent) calc(0.161 + 0.157 * var(--contrast)) calc(0.021 * var(--tint)) h);
  --border:        oklch(from var(--accent) 0.968 calc(0.017 * var(--tint)) h / 0.09);
  --border-strong: oklch(from var(--accent) 0.968 calc(0.017 * var(--tint)) h / 0.36);
  --text-primary:   oklch(from var(--accent) 0.951 calc(0.009 * var(--tint)) h);
  --text-secondary: oklch(from var(--accent) 0.820 calc(0.014 * var(--tint)) h);
  --text-on-accent: oklch(from var(--accent) 0.215 0.048 h);
  --char-rose: #F29DB9;
  --accent:             var(--char-rose);
  --accent-fill-hover:  color-mix(in oklch, var(--accent) 88%, black);
  --accent-wash:        color-mix(in oklch, var(--accent) 12%, transparent);
  --accent-wash-strong: color-mix(in oklch, var(--accent) 22%, transparent);
  --error:        #ED7665;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg-canvas);color:var(--text-primary)}
body{min-height:100vh;display:grid;place-items:center;padding:var(--s-4);
  font:var(--fw-regular) var(--t-md)/var(--lh-ui) var(--font-sans);-webkit-font-smoothing:antialiased}
main{width:100%;max-width:var(--dialog-sm);background:var(--surface-1);border-radius:var(--r-lg);padding:var(--s-6)}
h2{margin:0 0 var(--s-2);font-size:var(--t-xl);font-weight:var(--fw-bold)}
p{margin:0 0 var(--s-4);color:var(--text-secondary);line-height:var(--lh-body)}
label{display:block;margin:0 0 var(--s-2);font-weight:var(--fw-medium);color:var(--text-secondary)}
input{display:block;width:100%;height:var(--h-md);margin:0 0 var(--s-4);padding:0 var(--s-3);
  border:1px solid var(--border-strong);border-radius:var(--r-md);background:var(--surface-2);
  color:var(--text-primary);font:inherit}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
@media (pointer:coarse){input{font-size:max(1rem,var(--t-md))}}
#code{font-family:var(--font-mono);letter-spacing:.04em}
button{display:inline-flex;align-items:center;justify-content:center;height:var(--h-lg);padding:0 var(--s-5);
  border:0;border-radius:var(--r-md);font:var(--fw-medium) var(--t-md)/var(--lh-ui) var(--font-sans);cursor:pointer}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button:disabled{opacity:.6;cursor:progress}
.primary{width:100%;background:var(--accent);color:var(--text-on-accent)}
.primary:hover:not(:disabled){background:var(--accent-fill-hover)}
.secondary{background:var(--surface-3);color:var(--text-primary)}
.secondary:hover:not(:disabled){background:var(--surface-4)}
.link{height:auto;padding:var(--s-1) 0;margin-top:var(--s-4);background:none;color:var(--text-secondary);
  font-weight:var(--fw-regular)}
#v-login{display:flex;flex-direction:column;align-items:center}
.rat{width:112px;height:112px;padding:0;border-radius:var(--r-full);background:var(--accent-wash);
  color:var(--accent);transition:background var(--dur-fast),transform var(--dur-fast)}
.rat:hover:not(:disabled){background:var(--accent-wash-strong);transform:scale(1.05)}
.rat:active:not(:disabled){transform:scale(.97)}
.rat svg{width:60px;height:60px}
@media (prefers-reduced-motion:reduce){.rat{transition:none}.rat:hover:not(:disabled){transform:none}}
.link:hover{color:var(--text-primary)}
.row{display:flex;gap:var(--s-2);margin-top:var(--s-2)}.row .primary{flex:1}
form.method{width:100%;margin-top:var(--s-5)}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:var(--s-2);margin:0 0 var(--s-4)}
.card{height:auto;flex-direction:column;gap:var(--s-1);padding:var(--s-3) var(--s-2);background:var(--surface-2);
  color:var(--text-primary);border:1px solid var(--border);font-weight:var(--fw-medium)}
.card:hover:not(:disabled){background:var(--surface-3)}
.card.on{border-color:var(--accent);background:var(--accent-wash)}
.card small{font-size:var(--t-xs);font-weight:var(--fw-regular);color:var(--accent)}
.pin{display:flex;gap:var(--s-2);margin:0 0 var(--s-4)}
.pin input{flex:1;min-width:0;height:var(--h-lg);margin:0;padding:0;text-align:center;
  font:var(--fw-medium) var(--t-xl)/var(--lh-ui) var(--font-mono)}
ol{margin:0 0 var(--s-4);padding:var(--s-3) var(--s-4) var(--s-3) var(--s-7);background:var(--surface-2);
  border-radius:var(--r-md);font:var(--t-md)/var(--lh-body) var(--font-mono);color:var(--text-primary);
  white-space:nowrap}
.err{min-height:1.55em;margin:var(--s-3) 0 0;color:var(--error)}
.err:empty{display:none}
[hidden]{display:none!important}
/* wide screens: about 12 % larger, like the app (tokens.css) */
@media (min-width:1600px){main{zoom:1.15}}
</style>
</head>
<body>
<main id="celine-login">
  <section id="v-login" hidden>
    <button id="login" class="rat" type="button" aria-label="Log in with passkey" title="Log in with passkey" hidden>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"
           stroke-linejoin="round" aria-hidden="true">
        <path d="M17 5c0-1.7-1.3-3-3-3s-3 1.3-3 3c0 .8.3 1.5.8 2H11c-3.9 0-7 3.1-7 7c0 2.2 1.8 4 4 4"/>
        <path d="M16.8 3.9c.3-.3.6-.5 1-.7 1.5-.6 3.3.1 3.9 1.6.6 1.5-.1 3.3-1.6 3.9l1.6 2.8c.2.3.2.7.2 1-.2.8-.9 1.2-1.7 1.1 0 0-1.6-.3-2.7-.6H17c-1.7 0-3 1.3-3 3"/>
        <path d="M13.2 18a3 3 0 0 0-2.2-5"/><path d="M13 22H4a2 2 0 0 1 0-4h12"/><path d="M16 9h.01"/>
      </svg>
    </button>
    <form id="login-password" class="method" hidden>
      <label for="password">Password</label>
      <input id="password" type="password" autocomplete="current-password" required>
      <button class="primary" type="submit">Log in</button>
    </form>
    <form id="login-pin" class="method" hidden>
      <label>PIN</label>
      <div class="pin" id="pin-login"></div>
      <button class="primary" type="submit">Log in</button>
    </form>
    <button id="to-recovery" class="link" type="button">Use a recovery code</button>
  </section>

  <form id="v-code" hidden>
    <p id="code-hint"></p>
    <label for="code" id="code-label"></label>
    <input id="code" autocomplete="off" autocapitalize="characters" spellcheck="false" required>
    <div class="cards" role="radiogroup">
      <button class="card on" type="button" data-kind="passkey">Passkey<small>Recommended</small></button>
      <button class="card" type="button" data-kind="password">Password</button>
      <button class="card" type="button" data-kind="pin">PIN</button>
    </div>
    <div id="m-password" hidden>
      <label for="new-password">Password</label>
      <input id="new-password" type="password" autocomplete="new-password" minlength="10">
    </div>
    <div id="m-pin" hidden>
      <label>PIN</label>
      <div class="pin" id="pin-new"></div>
    </div>
    <button id="add" class="primary" type="submit">Add passkey</button>
    <button id="back" class="link" type="button" hidden>Back</button>
  </form>

  <section id="v-codes" hidden>
    <h2>Recovery codes</h2>
    <p>Save these codes somewhere safe. Each one works once, and the app does not show them again.</p>
    <ol id="codes"></ol>
    <div class="row">
      <button id="copy" class="secondary" type="button">Copy</button>
      <button id="done" class="primary" type="button">Continue</button>
    </div>
  </section>

  <p id="err" class="err" role="alert"></p>
</main>
<script>
const SETUP = __SETUP__
const METHODS = __METHODS__
const ORIGIN = __ORIGIN__
const $ = id => document.getElementById(id)
const enc = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
const dec = s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - s.length % 4) % 4)), c => c.charCodeAt(0))

function creationOptions(o) {
  return { ...o, challenge: dec(o.challenge), user: { ...o.user, id: dec(o.user.id) },
           excludeCredentials: (o.excludeCredentials || []).map(c => ({ ...c, id: dec(c.id) })) }
}
function requestOptions(o) {
  return { ...o, challenge: dec(o.challenge), allowCredentials: (o.allowCredentials || []).map(c => ({ ...c, id: dec(c.id) })) }
}
function credentialJSON(c) {
  const r = c.response, out = { id: c.id, rawId: enc(c.rawId), type: c.type,
    authenticatorAttachment: c.authenticatorAttachment || undefined,
    clientExtensionResults: c.getClientExtensionResults(), response: { clientDataJSON: enc(r.clientDataJSON) } }
  if (r.attestationObject) {
    out.response.attestationObject = enc(r.attestationObject)
    out.response.transports = r.getTransports ? r.getTransports() : []
  } else {
    out.response.authenticatorData = enc(r.authenticatorData)
    out.response.signature = enc(r.signature)
    if (r.userHandle) out.response.userHandle = enc(r.userHandle)
  }
  return out
}

async function post(path, body) {
  let r
  try {
    r = await fetch('/api/auth/' + path, { method: 'POST', headers: { 'content-type': 'application/json' },
                                           body: JSON.stringify(body || {}) })
  } catch { throw new Error('The server did not answer. Check your connection and try again.') }
  const j = await r.json().catch(() => ({}))
  if (!r.ok) {
    const e = j.error || {}
    if (e.code === 'locked' && e.locked_until)
      throw new Error('Too many failed attempts. Try again after ' + new Date(e.locked_until * 1000).toLocaleString() + '.')
    throw new Error(e.message || 'The request failed (HTTP ' + r.status + ').')
  }
  return j
}

function explain(e) {
  if (e && e.name === 'NotAllowedError') return 'You closed the passkey prompt, or it timed out.'
  if (e && e.name === 'InvalidStateError') return 'This passkey is already registered here.'
  if (e && e.name === 'SecurityError') return 'Passkeys work only at ' + ORIGIN + '. Open the app there to use one.'
  return (e && e.message) || 'The request failed.'
}

// eight single-digit boxes: typing moves on, backspace moves back, a paste fills them
function pinBoxes(el, onFull) {
  const boxes = Array.from({ length: 8 }, (_, i) => {
    const b = document.createElement('input')
    b.type = 'password'; b.inputMode = 'numeric'; b.maxLength = 1; b.autocomplete = 'off'
    b.setAttribute('aria-label', 'PIN digit ' + (i + 1))
    b.oninput = () => {
      b.value = b.value.replace(/\D/g, '').slice(-1)
      if (b.value && i < 7) boxes[i + 1].focus()
      if (onFull && value().length === 8) onFull()
    }
    b.onkeydown = ev => {
      if (ev.key === 'Backspace' && !b.value && i > 0) { boxes[i - 1].value = ''; boxes[i - 1].focus(); ev.preventDefault() }
      if (ev.key === 'ArrowLeft' && i > 0) boxes[i - 1].focus()
      if (ev.key === 'ArrowRight' && i < 7) boxes[i + 1].focus()
    }
    b.onpaste = ev => {
      const digits = (ev.clipboardData.getData('text') || '').replace(/\D/g, '').slice(0, 8)
      if (!digits) return
      ev.preventDefault()
      boxes.forEach((x, j) => { x.value = digits[j] || '' })
      boxes[Math.min(digits.length, 7)].focus()
      if (onFull && digits.length === 8) onFull()
    }
    return b
  })
  el.replaceChildren(...boxes)
  const value = () => boxes.map(b => b.value).join('')
  const clear = () => { boxes.forEach(b => { b.value = '' }); boxes[0].focus() }
  return { value, clear, focus: () => boxes[0].focus() }
}

function show(view) {
  for (const v of ['v-login', 'v-code', 'v-codes']) $(v).hidden = v !== view
  $('err').textContent = ''
}

let kind = 'passkey'
function pick(k) {
  kind = k
  for (const c of document.querySelectorAll('.card')) {
    c.classList.toggle('on', c.dataset.kind === k)
    c.setAttribute('aria-checked', String(c.dataset.kind === k))
  }
  $('m-password').hidden = k !== 'password'
  $('m-pin').hidden = k !== 'pin'
  $('new-password').required = k === 'password'
  $('add').textContent = k === 'passkey' ? 'Add passkey' : k === 'password' ? 'Add password' : 'Add PIN'
}

function codeView(recovery) {
  $('code-hint').textContent = recovery ? 'Enter a recovery code. Then add a new way to log in.'
                                        : 'Enter the setup code from setup-code.txt in the data folder. Then pick how you log in.'
  $('code-label').textContent = recovery ? 'Recovery code' : 'Setup code'
  $('back').hidden = !recovery
  pick('passkey')
  show('v-code')
  $('code').focus()
}

async function busy(button, fn) {
  button.disabled = true
  $('err').textContent = ''
  try { await fn() } catch (e) { $('err').textContent = explain(e) } finally { button.disabled = false }
}

const loginPin = pinBoxes($('pin-login'), () => $('login-pin').requestSubmit())
const newPin = pinBoxes($('pin-new'))

$('login').onclick = () => busy($('login'), async () => {
  const { ceremony, options } = await post('login/start')
  const cred = await navigator.credentials.get({ publicKey: requestOptions(options) })
  await post('login/finish', { ceremony, credential: credentialJSON(cred) })
  location.replace('/')
})

$('login-password').onsubmit = ev => { ev.preventDefault(); void busy(ev.submitter || $('login-password').lastElementChild, async () => {
  await post('login', { method: 'password', secret: $('password').value })
  location.replace('/')
}) }

$('login-pin').onsubmit = ev => { ev.preventDefault(); void busy($('login-pin').lastElementChild, async () => {
  try { await post('login', { method: 'pin', secret: loginPin.value() }) }
  catch (e) { loginPin.clear(); throw e }
  location.replace('/')
}) }

for (const c of document.querySelectorAll('.card')) c.onclick = () => pick(c.dataset.kind)

$('v-code').onsubmit = ev => { ev.preventDefault(); void busy($('add'), async () => {
  const code = $('code').value
  let res
  if (kind === 'passkey') {
    const { ceremony, options } = await post('register/start', { code })
    const cred = await navigator.credentials.create({ publicKey: creationOptions(options) })
    res = await post('register/finish', { ceremony, credential: credentialJSON(cred) })
  } else {
    const secret = kind === 'pin' ? newPin.value() : $('new-password').value
    res = await post('register/credential', { code, kind, secret })
  }
  if (res.recovery_codes) {
    $('codes').replaceChildren(...res.recovery_codes.map(c => Object.assign(document.createElement('li'), { textContent: c })))
    show('v-codes')
  } else location.replace('/')
}) }

$('to-recovery').onclick = () => codeView(true)
$('back').onclick = () => show('v-login')
$('copy').onclick = () => busy($('copy'), async () => {
  await navigator.clipboard.writeText([...$('codes').children].map(li => li.textContent).join('\n'))
  $('copy').textContent = 'Copied'
})
$('done').onclick = () => location.replace('/')

if (SETUP) codeView(false)
else {
  $('login').hidden = !METHODS.includes('passkey')
  $('login-password').hidden = !METHODS.includes('password')
  $('login-pin').hidden = !METHODS.includes('pin')
  show('v-login')
  if (METHODS.includes('passkey')) $('login').focus()
  else if (METHODS.includes('password')) $('password').focus()
  else loginPin.focus()
}
</script>
</body>
</html>
"""
