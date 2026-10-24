"""The Docker health check's own listener: 127.0.0.1:<[server] health_port> only.

With the login on, /api/health needs a session like everything else. The container's
health check asks this port instead. It binds loopback inside the container, so
nothing outside (the reverse proxy included) can reach it. It answers 200 while
`ok()` holds (the store's writer runs), else 503.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("celine.auth")

def _reply(ok: bool) -> bytes:
    body = b'{"ok":true}' if ok else b'{"ok":false}'
    return (b"HTTP/1.1 " + (b"200 OK" if ok else b"503 Service Unavailable")
            + b"\r\ncontent-type: application/json\r\ncontent-length: "
            + str(len(body)).encode() + b"\r\nconnection: close\r\n\r\n" + body)


async def _answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ok) -> None:
    try:
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        writer.write(_reply(ok()))
        await writer.drain()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
            ConnectionError):
        pass
    finally:
        writer.close()


async def serve(port: int, ok) -> None:
    """Runs until cancelled."""
    server = await asyncio.start_server(lambda r, w: _answer(r, w, ok), "127.0.0.1", port)
    log.info("health listener up", extra={"event": "health.listening", "port": port})
    async with server:
        await server.serve_forever()
