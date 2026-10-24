"""One artifact version as the browser gets it. The only place that knows the content
security policy and the library hosts.

A page runs sandboxed by its own header, so it holds when the address is opened in a
tab too: an opaque origin, no cookie, no app API, no storage, no parent window. The
frame in the app adds the same sandbox as its attribute. Nothing leaves but loads from
the library hosts and Google Fonts, with two gaps no header closes: WebRTC (Chrome 153
knows no directive for it), and a page open in a tab of its own may go to another
address with data in it (in the app's frame the app's own policy stops that). Measured
in Chrome 153, one page per library:
'unsafe-eval' is for Alpine, Vue's string templates and WebAssembly, worker-src for
Tone.js and other workers, the hosts in img-src for Leaflet's markers, connect-src
data: blob: for a page that fetches what it holds."""
from __future__ import annotations

# the library hosts: scripts, styles, fonts, images and media (Claude's list); Google
# Fonts for styles and fonts. A request to them carries nothing a script could not send
# in a script address anyway.
HOSTS = ("https://cdnjs.cloudflare.com/", "https://cdn.jsdelivr.net/npm/", "https://unpkg.com/",
         "https://cdn.tailwindcss.com/", "https://code.jquery.com/")
FONTS = ("https://fonts.googleapis.com/", "https://fonts.gstatic.com/")
LOADS_FROM = HOSTS + FONTS
SANDBOX = "allow-scripts allow-forms"
_HOSTS = " ".join(HOSTS)
CSP = "; ".join([
    f"sandbox {SANDBOX}",
    "default-src 'none'",
    f"script-src 'unsafe-inline' 'unsafe-eval' {_HOSTS}",
    "worker-src blob:",
    f"style-src 'unsafe-inline' {_HOSTS} {FONTS[0]}",
    f"font-src data: {_HOSTS} {FONTS[1]}",
    f"img-src data: blob: {_HOSTS}",
    f"media-src data: blob: {_HOSTS}",
    "connect-src data: blob:",
    "form-action 'none'",
    "base-uri 'none'",
    "frame-ancestors 'self'",
])
HEADERS = {"Content-Security-Policy": CSP, "X-Frame-Options": "SAMEORIGIN",
           "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
           "Cache-Control": "private, no-cache"}
# A page without style of its own still looks like the app: dark, its type, colours
# and spacing (web/src/styles/tokens.css). Every rule is :where(), no weight at all, so
# any rule of its own wins. The frame cannot load the app's fonts: the system's face.
BASE = " ".join("""
:where(html){color-scheme:dark;background:#0F0E11;color:#F4ECE6;
font:16px/1.55 system-ui,-apple-system,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased}
:where(body){margin:24px}
:where(h1,h2,h3,h4){line-height:1.2;letter-spacing:-0.005em;margin:1.4em 0 .5em}
:where(h1){font-size:1.75rem} :where(h2){font-size:1.375rem} :where(h3){font-size:1.125rem}
:where(p,ul,ol,dl,table,pre,blockquote,figure){margin:0 0 1em}
:where(a){color:#F29DB9}
:where(code,kbd,pre){font-family:ui-monospace,'SF Mono',Menlo,monospace;font-size:.9em}
:where(:not(pre)>code){background:#1D1A20;border-radius:4px;padding:1px 4px}
:where(pre){background:#0B0A0D;border-radius:12px;padding:12px 16px;overflow:auto;
box-shadow:inset 0 0 0 1px rgb(255 240 245/.07)}
:where(blockquote){margin-inline:0;padding-left:16px;border-left:2px solid #F29DB9;color:#CDBFB6}
:where(table){border-collapse:collapse}
:where(th,td){padding:8px 16px 8px 0;border-bottom:1px solid rgb(255 240 245/.07);text-align:left}
:where(th){color:#A39389;font-weight:580}
:where(hr){border:0;border-top:1px solid rgb(255 240 245/.07);margin:24px 0}
:where(button,input,select,textarea){font:inherit;color:inherit}
:where(button){background:#26222A;border:0;border-radius:12px;padding:8px 16px;cursor:pointer}
:where(button:hover){background:#302B34}
:where(input,select,textarea){background:#1D1A20;border:1px solid rgb(255 240 245/.34);border-radius:12px;padding:8px 12px}
:where(img,svg,video){max-width:100%}
::selection{background:rgb(242 157 185/.22)}
""".split())
# on the page's first line, so the lines of what it wrote keep their numbers
HEAD = ('<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<style>{BASE}</style>')


def render(content: str) -> tuple[bytes, dict[str, str]]:
    """The page's body and headers."""
    return (HEAD + content).encode(), {**HEADERS, "Content-Type": "text/html; charset=utf-8"}


def source(content: str) -> tuple[bytes, dict[str, str]]:
    """What it wrote, as plain text under the same headers: the Download, and a
    markdown document, which the app renders itself."""
    return content.encode(), {**HEADERS, "Content-Type": "text/plain; charset=utf-8"}
