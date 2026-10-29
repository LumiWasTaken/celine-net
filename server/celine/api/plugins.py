"""The plugins at runtime: one `/api/plugins` mount (before the /api catch-all) whose
routes each plugin's runner appends while it is on and removes when it stops, and the
runner itself (core/runners.py, started and stopped by its switch).

A runner, in order: `<data>/plugins/<id>/`; its file `<id>.db` opened through KBFile with
its `migrations/` (a plugin without them opens nothing); its routers under
`/api/plugins/<id>`; its `run = "module:coroutine"` (services, plugin), or waiting until
it stops. Stopped: its routes go, its file closes and stays on disk.
"""
from __future__ import annotations

from pathlib import Path

import anyio
from starlette.exceptions import HTTPException
from starlette.routing import Mount, Router

from ..kb.db import KBFile
from ..plugins import Plugin


async def _not_found(scope, receive, send) -> None:
    """The core's 404 (auth/gate.py not_found's shape, through errors.py)."""
    raise HTTPException(404, scope["path"])


def mount() -> tuple[Mount, Router]:
    """The `/api/plugins` mount and the router the runners fill."""
    routes = Router(default=_not_found)
    return Mount("/api/plugins", app=routes), routes


async def run(services, routes: Router, p: Plugin) -> None:
    data = Path(services.cfg.data_dir) / "plugins" / p.id
    mounted: list[Mount] = []
    try:
        if p.migrations is not None:
            await anyio.to_thread.run_sync(lambda: data.mkdir(parents=True, exist_ok=True))
            p.file = await anyio.to_thread.run_sync(KBFile, data / f"{p.id}.db", p.migrations)
        p.data = data
        for ref in p.manifest.get("routes", ()):
            m = Mount(f"/{p.id}", app=p.attr(ref))
            routes.routes.append(m)
            mounted.append(m)
        if ref := p.manifest.get("run"):
            await p.attr(ref)(services, p)
        await anyio.sleep_forever()
    finally:
        for m in mounted:
            routes.routes.remove(m)
        if p.file is not None:
            p.file.close()
        p.file = p.data = None
