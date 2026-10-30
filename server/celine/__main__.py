"""Entrypoint: preflight, then serve."""
from __future__ import annotations

import argparse
import contextlib
import functools
import logging
import os
import socket
import sys
from pathlib import Path

import anyio
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as config_mod
from .api import origin as origin_mod
from .api import kb as kb_api
from .api import connectors as connectors_api
from .api import identity as identity_api
from .api import llm as llm_api
from .api import forms as forms_api, settings as settings_api
from .api import her as her_api
from .api import commands as commands_api
from .api import attachments as attachments_api, ws
from .api import notebook as notebook_api
from .api import skills as skills_api
from .conversations import api as conversations_api, store as conversations, tree
from . import attachments, boot, keys, reload
from .backends import llm as llm_mod
from .core.services import Services
from .mcp import Supervisor
from .triggers import Triggers
from .db.migrate import migrate_all
from .core.store import Store
from .core.bus import EventBus
from .skills import build as build_registry
from .turns import store as turns
from .turns.dispatcher import Dispatcher
from .errors import install_error_handler, set_secrets
from .clock import use_zone
from .logs import setup as setup_logging
from .core.event_types import KB_CHANGED

log = logging.getLogger("celine")

STATIC_DIR = config_mod.STATIC_DIR     # the web build it serves (config.py)


def _refuse(message: str) -> None:
    print(f"celine: {message}", file=sys.stderr)
    raise SystemExit(2)


def _load_config(load=boot.load) -> config_mod.Config:
    try:
        return load()
    except config_mod.ConfigError as e:
        _refuse(str(e))


def own_data_dir(data_dir: Path) -> None:
    """What the app writes is its user's alone: umask 077 for this process, the data dir
    made 0700, or closed when it is its own and was open. What another container reads
    from the volume (keys/*.hash, connector-clients, searxng-settings.yml) is mounted as
    a file of its own, with the mode keys.py gives it, so the closed folder above it
    changes nothing there."""
    os.umask(0o077)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = data_dir.stat()
    if st.st_uid == os.getuid() and st.st_mode & 0o077:
        data_dir.chmod(0o700)


def preflight(argv: argparse.Namespace) -> config_mod.Config:
    # a second worker process means a second writer and broken event sequencing
    if int(os.environ.get("WEB_CONCURRENCY", "1")) != 1:
        _refuse("WEB_CONCURRENCY must be 1. Celine has a single writer task; a "
                "second worker process would break event sequencing.")

    cfg = _load_config(config_mod.load)    # how it is served, where its data lives
    from .skills import check_guides
    if bad := check_guides():
        _refuse(f"A skill guide is missing or misnamed: {bad}")
    if argv.port:
        reload.CLI_SERVER["port"] = argv.port
        cfg.server.update(reload.CLI_SERVER)

    try:
        own_data_dir(cfg.data_dir)
        for p in (cfg.blobs_dir, cfg.data_dir / "logs"):
            p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _refuse(f"The data dir {cfg.data_dir} is not writable ({e.strerror}). Set "
                f"CELINE_DATA_DIR or paths.data in config/celine.toml to a folder this "
                f"user may write.")
    setup_logging(cfg.data_dir / "logs", dev=argv.dev,
                  level="DEBUG" if argv.verbose else "INFO")

    # hard refusal: a shell-running API on a network without a login
    from .auth import startup_problem
    problem = startup_problem(cfg.server, cfg.host, cfg.port, argv.dev)
    if problem:
        _refuse(problem)

    with contextlib.closing(socket.socket()) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((cfg.host, cfg.port))
        except OSError:
            _refuse(f"{cfg.host}:{cfg.port} is already in use. Another Celine is "
                    f"probably running.")

    made = keys.ensure(cfg.data_dir)      # the stack's own keys (keys.py)
    set_secrets(keys.values(cfg.data_dir), "keys")
    if made:
        log.info("keys made", extra={"event": "keys.made", "files": made,
                                     "dir": str(cfg.data_dir / "keys")})
    from .settings import files as settings_files, seed
    seed.seed(settings_files.settings_dir(cfg))    # the context shares: copied once
    version = migrate_all(cfg.db_path)
    cfg = _load_config()                            # everything, its row too
    cfg.server.update(reload.CLI_SERVER)
    set_secrets(cfg.secrets())
    use_zone(cfg)
    log.info("database ready", extra={"event": "db.ready", "version": version,
                                     "path": str(cfg.db_path)})

    for problem in cfg.problems:
        log.warning("configuration problem", extra={"event": "config.problem",
                                                   "file": problem.file,
                                                   "problem": problem.message})
    return cfg


def build_app(cfg: config_mod.Config, dev: bool) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with anyio.create_task_group() as tg:
            services = app.state.services
            bus = services.bus = EventBus()
            store = services.store = Store(cfg.db_path, bus=bus)
            await store.start(tg)
            from .conversations.store import keep_lock
            keep_lock(store, cfg)               # Lock archived chats, where its SQL reads it
            # a fresh install's row of it; then the config again, with it
            from . import her
            if await store.write(her.seed):
                reload.refill(cfg)
            # the one LLM queue and router, before anything that asks a model (document
            # descriptions start with the first scan, captions with their scheduler)
            llm = services.llm = llm_mod.LLM(cfg, store.emit_global)
            from .settings import features     # what is on now (hot: core/runners.py)
            from .core.runners import Runner, Runners
            on = features.effective(cfg.settings.features)
            runners = services.runners = Runners()

            import asyncio
            from .kb import KBs
            kbs = services.kbs = await anyio.to_thread.run_sync(
                lambda: KBs(cfg, store.emit_global, llm))
            kbs.tasks.bind(asyncio.get_running_loop())
            from .kb import embed                     # index builds wait in the LLM queue
            embed.use_queue(llm.scheduler, asyncio.get_running_loop())
            from .kb import semantic as _semantic     # semantic topic search index updates
            tg.start_soon(_semantic.run_scheduler, cfg, kbs)
            from .kb import media as _media           # image captions (Settings, General)
            tg.start_soon(_media.run_scheduler, cfg, kbs)
            log.info("knowledge bases ready", extra={"event": "kb.ready",
                                                     "kbs": sorted(kbs.all)})

            # its vault: logged in in the background, locked until the user types the
            # master password (Settings, Vault); its turns wait for the login (the
            # dispatcher's ready)
            from .vault import Vault
            from .approvals import Approvals
            services.vault = Vault(cfg, store.emit_global)
            services.approvals = Approvals(cfg, store.emit_global, store)
            services.vault.forget = lambda: services.approvals.forget("vault")

            async def vault_run() -> None:
                try:
                    await services.vault.connect()
                finally:                     # its turns wait on it (dispatcher.ready)
                    services.vault.ready.set()
            runners["vault"] = Runner(tg, vault_run)
            if on["vault"]:
                await runners["vault"].start()
            else:
                services.vault.ready.set()

            registry = services.registry = build_registry(services)
            from .settings import conversations as conv_models
            await conv_models.load_all(store)      # per-conversation chat models
            from .settings import roles as conv_roles
            await conv_roles.load_all(store, cfg)  # per-conversation roles
            if "notebook" in registry.skills:
                await registry.skills["notebook"].load_marks(store)
                seeded = await registry.skills["notebook"].seed(store)
                if seeded:
                    log.info("notebook seeded", extra={"event": "notebook.seeded",
                                                       "files": seeded})
                fixed = await registry.skills["notebook"].reconcile(store)
                if any(fixed.values()):
                    log.info("notebook reconciled", extra={"event": "notebook.reconciled",
                                                           **fixed})
            log.info("skills ready", extra={"event": "skills.ready",
                                            "skills": len(registry.skills),
                                            "tools": len(registry.tools)})
            mcp = services.mcp = Supervisor(cfg, store, registry, kbs=kbs)
            await mcp.start(tg)
            # Discord and WhatsApp: the connectors' feeds and states (kb/connectors.py);
            # each with its MCP server and its archive under its feature's runner
            from .kb.connectors import Connectors
            connectors = services.connectors = Connectors(
                cfg, kbs, store.emit_global)
            tg.start_soon(connectors.run)

            def connector_runner(f: features.Feature) -> Runner:
                async def run() -> None:
                    for kid in f.kbs:
                        kbs.open(kid)
                    for sid in f.mcp:
                        connectors.open(sid)
                    try:
                        async with anyio.create_task_group() as g:
                            for sid in f.mcp:
                                g.start_soon(mcp.run_one, cfg.mcp.get(sid))
                    finally:
                        for sid in f.mcp:
                            connectors.close(sid)
                        for kid in f.kbs:
                            kbs.close(kid)
                return Runner(tg, run)
            for f in features.BASE:          # a plugin's servers run under its own runner
                if f.mcp:
                    runners[f.id] = connector_runner(f)
                    if on[f.id]:
                        await runners[f.id].start()

            # before the workers start: what a crash or restart left behind
            from .comfyui import batches as comfy_batches, server as comfy_server
            if on["images"] or on["music"]:
                tg.start_soon(comfy_server.free_shared, cfg, llm)
            tg.start_soon(llm.router.watch)     # the host ping; paused without one

            async def media_run() -> None:     # images and music: the batches
                await comfy_batches.sweep(store)
                async with anyio.create_task_group() as g:
                    services.batches = comfy_batches.Batches(services, g)
                    try:
                        await anyio.sleep_forever()
                    finally:
                        services.batches = None
            runners["media"] = Runner(tg, media_run, features=("images", "music"))
            if on["images"] or on["music"]:
                await runners["media"].start()
            await attachments.gc(cfg, store)
            await conversations.prune_events(store, keep=int(cfg.r("events_keep")),
                                             requests_days=int(cfg.r("requests_keep")))
            tg.start_soon(conversations.keep_pruned, store, cfg)    # and every so often
            swept = await turns.sweep(store, registry)
            from .approvals import close_left
            await close_left(store)                 # the chats' requests of the last run
            if swept["total"]:
                log.info("swept orphans", extra={"event": "startup.swept",
                                                 "count": swept["total"],
                                                 "interrupted": swept["interrupted"]})
            dispatcher = services.dispatcher = Dispatcher(store, cfg, bus, None,
                                                          registry, llm=llm)
            dispatcher.ready = services.vault.ready
            tree.prefills = lambda cid: conversations_api.prefills(services, cid)

            async def describe_again(p: dict) -> None:   # kb/documents_describe.py
                if (kb := kbs.get(p["kb"])) is not None:
                    await anyio.to_thread.run_sync(kb.kick_describe)

            async def documents_run() -> None:   # Documents: the folder, its index, its watcher
                for kid in features.get("documents").kbs:
                    await anyio.to_thread.run_sync(kbs.open, kid)
                try:
                    dispatcher.work.handle("documents.describe", ("small",), describe_again)
                    dispatcher.work.notify()
                    if (documents := kbs.documents()) is not None:
                        documents.work = dispatcher.work
                        scanned = await anyio.to_thread.run_sync(documents.rescan)
                        log.info("documents scanned",
                                 extra={"event": "documents.scanned", **scanned})
                        await _watch_documents(documents, store)
                    await anyio.sleep_forever()
                finally:
                    for kid in features.get("documents").kbs:
                        kbs.close(kid)
            runners["documents"] = Runner(tg, documents_run)
            if on["documents"]:
                await runners["documents"].start()
            from .push import Push              # the notices to devices without a focused page
            push = services.push = Push(cfg, store, bus, app.state.auth)
            tg.start_soon(push.run)
            from .intercom import Intercom      # messages between everyone, and their timer
            intercom = services.intercom = Intercom(store, dispatcher.notify, dispatcher.user_wrote)
            dispatcher.intercom = intercom       # a Stop's cascade cancels schedules
            # every on_end before the workers start: a turn resumed at start that ends
            # while the awaits below run still reports and answers its pair
            from .sub_agents import report       # a sub-agent that is done reports
            dispatcher.on_end(lambda cid, _turn: report.on_end(services, cid))
            from .intercom import answer         # its answer to the user's reply, in its thread
            dispatcher.on_end(lambda cid, turn: answer.on_end(services, cid, turn))
            from . import leftovers              # a done mission's terminals and tabs close
            dispatcher.on_end(lambda cid, _turn: leftovers.on_end(services, cid))
            from .missions import watch as mission_watch    # a mission keeps going
            mission_watch.start(services)
            from .skills import mind                 # its mood: the appraisal after a turn
            mind.start(services)
            await dispatcher.start(tg)
            await intercom.run(tg)
            from .sub_agents.skill import release_all     # queued before a restart
            await release_all(services)
            from .schedules.runs import Scheduler   # the schedules' runs
            scheduler = Scheduler(services)          # made once: its turn-end hook

            async def routines_run() -> None:       # the skills' triggers, the schedules
                try:
                    async with anyio.create_task_group() as g:
                        services.triggers = Triggers(services)
                        await services.triggers.run(g)
                        services.schedules = scheduler
                        await scheduler.run(g)
                        await anyio.sleep_forever()
                finally:
                    services.triggers = services.schedules = None

            async def guard_run() -> None:          # Bastet: its answers, its patrol
                from .guard import patrol, store as guard_rows, talk
                await guard_rows.close_asked(store)     # its asks of the last run
                talk.start(services)                    # it answers her on the Intercom
                dispatcher.work.notify()
                try:
                    await patrol.run(services)
                finally:
                    for kind in (talk.KIND, talk.NOTE):
                        dispatcher.work.handlers.pop(kind, None)
            runners["routines"] = Runner(tg, routines_run, on_stop=scheduler.forget)
            runners["guard"] = Runner(tg, guard_run)
            runners["missions"] = Runner(tg, lambda: mission_watch.run(services))  # check-ins
            for fid in ("routines", "guard", "missions"):
                if on[fid]:
                    await runners[fid].start()
            # its terminals, for the pages: the routes always have the sync, its loop
            # follows the shellbox only when there is one
            from .terminals.sync import TerminalSync
            services.terminals = TerminalSync(cfg, store)
            services.terminals.ready = services.vault.ready
            runners["shellbox"] = Runner(tg, services.terminals.run)
            from .browser import sync as browser_sync    # its browser: pages hear browser.state
            runners["browser"] = Runner(tg, lambda: browser_sync.watch(cfg, store))
            for fid in ("shellbox", "browser"):
                if on[fid]:
                    await runners[fid].start()
            from . import plugins                # each plugin's runner, by its switch
            from .api import plugins as plugins_api
            for p in plugins.all():
                runners[p.id] = Runner(tg, functools.partial(
                    plugins_api.run, services, app.state.plugin_routes, p))
                if on[p.id]:
                    await runners[p.id].start()
            tg.start_soon(reload.watch, services)

            auth = app.state.auth
            if auth is not None:
                auth.store.prune_sessions()
                auth.announce_setup_code()       # no passkey yet: a one-time code
            if cfg.server.get("health_port"):
                from .auth import health
                tg.start_soon(health.serve, int(cfg.server["health_port"]), lambda: store.alive)
            if cfg.server.get("metrics_port"):
                from . import metrics
                tg.start_soon(metrics.serve, int(cfg.server["metrics_port"]))
            log.info("celine is up", extra={"event": "startup.ready",
                                            "url": f"http://{cfg.host}:{cfg.port}",
                                            "login": "on" if auth else "off"})
            try:
                yield
            finally:
                embed.use_queue(None, None)    # its loop ends: builds after it take no ticket
                await _announce_shutdown(store, intercom)
                await dispatcher.stop()
                await store.aclose()
                tg.cancel_scope.cancel()

    app = FastAPI(title="Celine", lifespan=lifespan, docs_url=None, redoc_url=None)
    # everything the app runs, filled by the lifespan as each part starts (core/services.py)
    app.state.services = Services(cfg=cfg)
    install_error_handler(app)
    from . import auth as auth_mod
    from .auth.gate import not_found
    from .auth.install import install as install_login
    from .settings import address
    origin_mod.install(app, cfg.port, dev, log, server=cfg.server,
                       login_on=auth_mod.mode(cfg.server) == "on",
                       stored=lambda: address.get(cfg))
    install_login(app, cfg, dev)             # the session gate
    from .api.headers import SecurityHeaders
    app.add_middleware(SecurityHeaders)      # outermost: covers the gate's answers too
    app.include_router(attachments_api.router)
    app.include_router(notebook_api.router)
    app.include_router(skills_api.router)
    from .api import context as context_api
    app.include_router(context_api.router)
    from .jobs import api as jobs_api
    app.include_router(jobs_api.router)
    app.include_router(conversations_api.router)
    app.include_router(kb_api.router)
    app.include_router(connectors_api.router)
    app.include_router(identity_api.router)
    app.include_router(llm_api.router)
    from .api import llm_proxy
    app.include_router(llm_proxy.router)
    app.include_router(settings_api.router)
    app.include_router(forms_api.router)
    from .api import speech as speech_api
    app.include_router(speech_api.router)
    app.include_router(her_api.router)
    app.include_router(commands_api.router)
    from .api import documents as documents_api
    app.include_router(documents_api.router)
    from .api import bulk as bulk_api
    app.include_router(bulk_api.router)
    from .missions import api as missions_api
    app.include_router(missions_api.router)
    from .triggers import api as triggers_api
    app.include_router(triggers_api.router)
    from .schedules import api as schedules_api
    app.include_router(schedules_api.router)
    from .sub_agents import api as sub_agents_api
    app.include_router(sub_agents_api.router)
    from .intercom import api as intercom_api
    app.include_router(intercom_api.router)
    from .terminals import api as terminals_api
    app.include_router(terminals_api.router)
    app.include_router(terminals_api.ws_router)
    from .browser import api as browser_api
    app.include_router(browser_api.router)
    app.include_router(browser_api.ws_router)
    from .artifacts import api as artifacts_api
    app.include_router(artifacts_api.router)
    from . import shares as shares_api
    app.include_router(shares_api.router)
    from . import push as push_api
    app.include_router(push_api.router)
    from .comfyui import api as comfyui_api
    app.include_router(comfyui_api.router)
    from .api import presets as presets_api
    app.include_router(presets_api.router)
    from .api import appearance as appearance_api, texts as texts_api
    app.include_router(appearance_api.router)
    app.include_router(texts_api.router)
    from .guard import api as guard_api
    app.include_router(guard_api.router)
    from .api import approvals as approvals_api
    app.include_router(approvals_api.router)
    from .bugreports import api as bugreports_api
    app.include_router(bugreports_api.router)
    from . import events as events_api
    app.include_router(events_api.router)
    app.include_router(ws.router)

    # the plugins' routes, appended and removed by their runners (api/plugins.py)
    from .api import plugins as plugins_api
    plugins_mount, app.state.plugin_routes = plugins_api.mount()
    app.router.routes.append(plugins_mount)

    @app.get("/api/health", include_in_schema=False)
    async def _health() -> dict:
        return {"ok": True}

    # before the SPA catch-all, or an unknown /api path returns HTML; every method, or an
    # unknown POST matches this path's GET and answers 405. The login's gate answers
    # exactly this for everything while there is no session.
    @app.api_route("/api/{rest_of_path:path}", include_in_schema=False,
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def _api_404(rest_of_path: str):
        return not_found(f"/api/{rest_of_path}")

    if (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"),
                  name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa(full_path: str):
            # a file of web/public at the root of the build (the PWA manifest, its icons, theme.js)
            f = STATIC_DIR / full_path
            if "/" not in full_path and f.suffix in _ROOT_FILES and f.is_file():
                return FileResponse(f, headers={"cache-control": "no-cache"})
            # never from the browser cache: after a logout the same URL must reach the
            # gate and get the login page (the assets are content-hashed and may cache)
            return FileResponse(STATIC_DIR / "index.html", headers={"cache-control": "no-store"})
    else:
        @app.get("/", include_in_schema=False)
        async def _no_build():
            return JSONResponse({
                "message": "The frontend is not built. Run `make web` for the dev "
                           "server, or `make build` to bundle it here.",
            })

    return app


# what web/public may put at the root of the build
_ROOT_FILES = {".webmanifest", ".png", ".svg", ".ico", ".js"}


async def _announce_shutdown(store, intercom) -> None:
    """Every conversation with a job at work hears that the app restarts: it finds it in
    its next turn. What the restart cuts comes after the start (the sweep); a turn it
    cuts ends `interrupted` and waits for the user's Regenerate."""
    try:
        cids = await store.read(lambda c: [r[0] for r in c.execute(
            "SELECT DISTINCT conversation_id FROM jobs WHERE state='running'")])
        for cid in cids:
            await intercom.send("system:app", f"agent:{cid}", "The app is restarting now.",
                                kind="notice", wake=False)
    except Exception:
        log.exception("restart notice failed", extra={"event": "shutdown.notice_failed"})


async def _watch_documents(documents, store) -> None:
    """Edits made outside the app (an editor, git) reach the index within a second."""
    import watchfiles
    try:
        async for _ in watchfiles.awatch(documents.root, debounce=800, recursive=True,
                                          rust_timeout=500):
            try:
                changed = await anyio.to_thread.run_sync(documents.rescan)
            except Exception:
                log.exception("documents rescan failed")
                continue
            if any(changed.get(k) for k in ("new", "changed", "moved", "removed")):
                store.emit_global({"type": KB_CHANGED, "kb": documents.id})
    except Exception:
        log.exception("documents watcher stopped")


def main() -> None:
    ap = argparse.ArgumentParser(prog="celine")
    ap.add_argument("--dev", action="store_true",
                    help="allow the Vite dev origin and log human-readably")
    ap.add_argument("--port", type=int, help="override [server] port")
    ap.add_argument("--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("doctor", help="check config, database and backend, then exit")
    exp = sub.add_parser("export", help="write every conversation as JSON and Markdown")
    exp.add_argument("--out", type=Path, help="directory (default Documents/exports/export-<time>)")
    pur = sub.add_parser("purge", help="delete and ignore every chat under N messages, "
                                       "then the people nothing refers to")
    pur.add_argument("--under", type=int, required=True, help="messages a chat needs to stay")
    pur.add_argument("--dry-run", action="store_true", help="print the counts, change nothing")
    kys = sub.add_parser("keys", help="make the stack's keys under <dir>/keys when missing "
                                      "(default: the data dir), then exit")
    kys.add_argument("dir", nargs="?", type=Path)
    argv = ap.parse_args()

    if argv.command == "keys":
        from . import ops
        raise SystemExit(ops.keys(argv.dir or _load_config(config_mod.load).data_dir))

    if argv.command in ("doctor", "export", "purge"):
        from . import ops
        cfg = _load_config()
        set_secrets(cfg.secrets())
        set_secrets(keys.values(cfg.data_dir), "keys")
        own_data_dir(cfg.data_dir)
        migrate_all(cfg.db_path)
        if argv.command == "doctor":
            raise SystemExit(ops.doctor(cfg))
        if argv.command == "purge":
            raise SystemExit(ops.purge(cfg, argv.under, argv.dry_run))
        ops.export(cfg, argv.out)
        return

    cfg = preflight(argv)
    app = build_app(cfg, argv.dev)
    print(f"\n  http://{cfg.host}:{cfg.port}\n", file=sys.stderr)
    # forwarded client addresses are believed only from the configured proxies
    proxies = cfg.server.get("trusted_proxies") or []
    proxies = [proxies] if isinstance(proxies, str) else [str(p) for p in proxies]
    # the one socket (api/ws.py): a client sends only small control frames; a ping
    # every 20 s finds a dead connection behind a proxy
    # the standard loop, not uvloop: libuv resolves names on four threads, and a few
    # names that do not exist (a box not started) take seconds each to fail, so the
    # probes of the boxes that do run would wait behind them and time out
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning", loop="asyncio",
                access_log=False, proxy_headers=bool(proxies),
                forwarded_allow_ips=",".join(proxies) if proxies else None,
                ws_max_size=1 << 20, ws_ping_interval=20.0, ws_ping_timeout=20.0)


if __name__ == "__main__":
    main()
