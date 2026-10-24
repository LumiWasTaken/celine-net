"""discord-link --state var/discord-link --listen 127.0.0.1:8768 --clients-file FILE

Clients come from the clients file (the app's keys/connector-clients, docker-compose.yml):
"name:key:role[,...]" (commas or lines), role owner or reader. The token lives in
<state>/token (a QR login through /admin/login).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path


def setup_logging() -> None:
    """INFO for the link itself; httpx and httpcore, which log every request line,
    only warnings."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    ap = argparse.ArgumentParser(prog="discord-link")
    ap.add_argument("--state", default="var/discord-link", help="state directory")
    ap.add_argument("--listen", default="127.0.0.1:8768", help="host:port")
    ap.add_argument("--sync-every-minutes", type=float, default=360)
    ap.add_argument("--clients-file", type=Path, default=Path("/data/connector-clients"),
                    help="who may connect: name:key:role per line or comma separated")
    args = ap.parse_args()
    setup_logging()

    from .link import Link, parse_clients
    from .state import Locked, State
    try:
        clients = parse_clients(args.clients_file.read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"discord-link: {args.clients_file}: {e}")
    if not clients:
        sys.exit(f"discord-link: {args.clients_file} names no client (name:key:role[,...])")
    try:
        state = State(Path(args.state))
    except Locked as e:
        sys.exit(f"discord-link: {e}")
    host, _, port = args.listen.rpartition(":")
    link = Link(state, clients=clients, sync_every_minutes=args.sync_every_minutes)
    asyncio.run(_serve(link, host or "127.0.0.1", int(port)))


async def _serve(link, host: str, port: int) -> None:
    import uvicorn

    from .app import build
    server = uvicorn.Server(uvicorn.Config(build(link), host=host, port=port,
                                           log_level="warning", access_log=False))
    runner = asyncio.create_task(link.run())
    try:
        await server.serve()
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        link.state.close()


if __name__ == "__main__":
    main()
