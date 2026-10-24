"""Security headers on every HTTP answer, the login gate's included.

The app loads nothing from other sites. Scripts come only from /assets; styles also
inline (index.html has one <style>, React and the QR code write style attributes).
`frame-ancestors 'none'` keeps the app out of every frame, whatever X-Frame-Options a
proxy on the way sets. A response that brings its own Content-Security-Policy (the
login page, a sandboxed file) keeps it. One kind of answer runs script it did not ship:
its artifact pages (artifacts/page.py), sandboxed by their own header into an opaque
origin, framed only by the app's own pages.
"""
from __future__ import annotations

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
HEADERS = {
    b"content-security-policy": CSP.encode(),
    b"x-frame-options": b"DENY",
    b"x-content-type-options": b"nosniff",
}


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def wrapped(message):
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in HEADERS.items() if k not in have]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)
        return await self.app(scope, receive, wrapped)
