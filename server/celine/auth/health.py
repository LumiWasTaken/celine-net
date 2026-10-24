"""The Docker health check's own listener: 127.0.0.1:<[server] health_port> only.

With the login on, /api/health needs a session like everything else. The container's
health check asks this port instead. It binds loopback inside the container, so
nothing outside (the reverse proxy included) can reach it. It answers every request
with 200 once the app has started.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("celine.auth")

_BODY = b'{"ok":true}'
_REPLY = (b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: "
          + str(len(_BODY)).encode() + b"\r\nconnection: close\r\n\r\n" + _BODY)


async def _answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        writer.write(_REPLY)
        await writer.drain()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
            ConnectionError):
        pass
    finally:
        writer.close()


async def serve(port: int) -> None:
    """Runs until cancelled."""
    server = await asyncio.start_server(_answer, "127.0.0.1", port)
    log.info("health listener up", extra={"event": "health.listening", "port": port})
    async with server:
        await server.serve_forever()
