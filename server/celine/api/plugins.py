"""The plugins at runtime: one `/api/plugins` mount (before the /api catch-all) whose
routes each plugin's runner appends while it is on and removes when it stops, and the
runner itself (core/runners.py, started and stopped by its switch).

A runner, in order: `<data>/plugins/<id>/`; its file `<id>.db` opened through KBFile with
its `migrations/` (a plugin without them opens nothing); its routers under
`/api/plugins/<id>`; `features.changed` {id, running: true} on the global feed (its
form's actions can run now: api/forms.py); its `[servers.<id>]` MCP servers, filled from
its form (mcp.plugin_servers), each supervised beside its `run = "module:coroutine"`
(services, plugin), or waiting until it stops. Stopped: its servers and their tools go,
its routes go, its file closes and stays on disk, and `features.changed` says {id,
running: false}.
"""
from __future__ import annotations

from pathlib import Path

import anyio
from starlette.exceptions import HTTPException
from starlette.routing import Mount, Router

from ..core.event_types import FEATURES_CHANGED
from ..kb.db import KBFile
from ..mcp import plugin_servers
from ..plugins import Plugin


async def _not_found(scope, receive, send) -> None:
    """The core's 404 (auth/gate.py not_found's shape, through errors.py)."""
    raise HTTPException(404, scope["path"])


def mount() -> tuple[Mount, Router]:
    """The `/api/plugins` mount and the router the runners fill."""
    routes = Router(default=_not_found)
    return Mount("/api/plugins", app=routes), routes


def _say(services, p: Plugin) -> None:
    if services.store is not None:
        services.store.emit_global({"type": FEATURES_CHANGED, "id": p.id,
                                    "running": p.data is not None})


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
        _say(services, p)
        servers, _ = plugin_servers(services.cfg, p)
        async with anyio.create_task_group() as g:
            for scfg in servers.values():
                g.start_soon(services.mcp.run_one, scfg, p.dir / "guides")
            if ref := p.manifest.get("run"):
                await p.attr(ref)(services, p)
            await anyio.sleep_forever()
    finally:
        for m in mounted:
            routes.routes.remove(m)
        if p.file is not None:
            p.file.close()
        running, p.file, p.data = p.data is not None, None, None
        if running:
            _say(services, p)
