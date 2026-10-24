"""Wiring into the FastAPI app (called by __main__.build_app)."""
from __future__ import annotations

from fastapi import APIRouter

from . import Auth, mode, settings
from .gate import Gate
from .routes import open_router, router

_off = APIRouter(prefix="/api/auth")


@_off.get("/status")
async def _status_off() -> dict:
    return {"login": False}


def install(app, cfg, dev: bool) -> Auth | None:
    """Login on: the gate goes around everything and the login routes are added.
    Login off: only GET /api/auth/status, which says so. Returns the Auth, also kept on
    app.state.auth."""
    app.state.auth = None
    if mode(cfg.server) == "off":
        app.include_router(_off)
        return None
    auth = Auth.open(cfg.data_dir, settings(cfg.server, cfg.port, dev, rp_name=cfg.name,
                                            user_name=cfg.user))
    app.state.auth = auth
    app.include_router(open_router)
    app.include_router(router)
    app.add_middleware(Gate, auth=auth)      # added last: the outermost layer
    return auth
