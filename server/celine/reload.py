"""Config reload on file change. Values change in place so every holder of the
Config object sees them; a broken file keeps the last good values."""
from __future__ import annotations

import dataclasses
import logging

import watchfiles

from . import boot, config as config_mod
from .errors import set_secrets
from .skills import build as build_registry, names as skill_names
from .clock import use_zone
from .core.event_types import SETTINGS_CHANGED

log = logging.getLogger("celine.config")

RESTART_ONLY = ("mcp", "server", "data_dir")
WAIT_MS = 500           # a burst of file events within this is one reload
CLI_SERVER: dict = {}    # --port and friends, so a reload does not read them as a change


async def watch(app) -> None:
    """config/ and var/settings/ (what the Settings screens write, or you by hand); its
    row is written only through the app (celine.her)."""
    from .settings.files import settings_dir
    sdir = settings_dir(app.cfg)
    sdir.mkdir(parents=True, exist_ok=True)
    async for _ in watchfiles.awatch(config_mod.CONFIG_DIR, sdir, debounce=WAIT_MS,
                                     rust_timeout=WAIT_MS):
        await file_changed(app)


async def file_changed(app) -> None:
    """A save in Settings was applied already; its file landing here changes nothing. A
    hand edit is applied like one made in the app: the runners follow the features."""
    if apply(app, only_if_changed=True):
        from .settings import views
        await views.apply_runners(app)
        app.store.emit_global({"type": SETTINGS_CHANGED, "what": ["files"]})


def _fresh():
    fresh = boot.load()
    fresh.server.update(CLI_SERVER)
    return fresh


def _copy_into(cfg, fresh) -> None:
    for f in dataclasses.fields(fresh):
        if f.name not in RESTART_ONLY:
            setattr(cfg, f.name, getattr(fresh, f.name))


def refill(cfg) -> None:
    """The config read again into `cfg` at startup, before anything is built from it:
    after a fresh install's row of it was written."""
    _copy_into(cfg, _fresh())


def apply(app, *, only_if_changed: bool = False) -> bool:
    """Load the config again and copy it into `app.cfg`, then rebuild the skills. A
    rebuilt skill reads the running app (`app`) when a tool runs, so it needs nothing
    handed over. False: refused (a fatal problem), or `only_if_changed` and nothing
    differs from what is in use."""
    cfg, registry = app.cfg, app.registry
    try:
        fresh = _fresh()
    except Exception as e:      # a file that does not parse, or a bug: the last good stays
        log.warning("config reload failed: %s", e, extra={"event": "config.reload_failed"},
                    exc_info=not isinstance(e, config_mod.ConfigError))
        cfg.problems = [config_mod.Problem("config", str(e), fatal=True), config_mod.Problem(
            "config", "the last good configuration is still in use")]
        return False
    if any(p.fatal for p in fresh.problems):
        cfg.problems = fresh.problems + [config_mod.Problem(
            "config", "the last good configuration is still in use")]
        log.warning("config reload refused", extra={"event": "config.reload_refused"})
        return False

    if only_if_changed and not any(p.fatal for p in cfg.problems) and all(
            getattr(cfg, f.name) == getattr(fresh, f.name)
            for f in dataclasses.fields(fresh) if f.name != "problems"):
        return False
    restart = [f for f in RESTART_ONLY if getattr(cfg, f) != getattr(fresh, f)]
    _copy_into(cfg, fresh)
    for f in restart:
        cfg.problems.append(config_mod.Problem(
            "config", f"{f} changed; restart Celine to apply it"))

    set_secrets(cfg.secrets())
    use_zone(cfg)
    if app.store is not None:               # Lock archived chats, where its SQL reads it
        from .conversations.store import keep_lock
        keep_lock(app.store, cfg)
    built = build_registry(app)
    for name in skill_names():
        new, old = built.skills.get(name), registry.skills.get(name)
        if new is None:
            registry.remove(name)
            continue
        if old is not None and hasattr(new, "carry"):    # what the old one kept in memory
            new.carry(old)
        registry.add(new)
    if app.llm is not None:                 # a turn waiting for a model looks again
        app.llm.router.settings_changed()
    if app.dispatcher is not None:          # Turns at once (Settings, Advanced)
        app.dispatcher.resize()
    if app.triggers is not None:            # the rebuilt skills, the new state
        app.triggers.apply()
    if app.schedules is not None:           # changed schedules, maybe due now
        app.schedules.wake()
    log.info("config reloaded", extra={"event": "config.reloaded",
                                       "problems": len(cfg.problems)})
    return True


async def commit(services, changes: dict) -> dict:
    """Apply `changes` (section -> whole new value) on top of the current settings:
    validated, written, reloaded in place, published. Hold settings.write.lock(). The
    features' probes are asked anew after it: a page that reloads on the event reads the
    states the new settings give."""
    from .settings import write
    written = write.save(services.cfg, changes)
    applied = apply(services)
    services.feature_probes = None
    write.publish(services, sorted(set(changes) | set(written)))
    return {"applied": applied}
