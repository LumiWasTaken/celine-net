"""Batches: the one way images and music are made.

A batch is a list of items, rendered in order under one GPU lease. Celine starts one
with its generate tool, the user with POST /api/batches;
either way it runs in the app's task group, no job and with no deadline but each item's
ComfyUI timeout.
`create` checks every item and stores the batch with one slot per item: an item that
cannot be made (an unknown preset, a size outside the limits, a source it may not
use) is failed in its slot at once with the sentence why, and the rest go on. `run`
renders the items that wait, one after the other:

1. Before the first item of a ComfyUI provider: its GPU lease, when the provider shares
   its GPU with a llama-swap provider (backends/llm.py `gpu_lease`): this app's
   requests there finish, new ones wait, llama-swap unloads its models. The lease is
   held to the end of the batch: one unload and one reload per batch.
2. Each item: an edit uploads its source image (POST /upload/image), the workflow is
   queued (POST /prompt), followed until it ends, and what it made is fetched
   (server.run). A failed item (ComfyUI refused or failed, the timeout, a model or LoRA
   the catalog lacks) is one failed slot; the next item goes on.
3. What an item made is an attachment: of the message that holds its call (its image
   list and edits find it), or of the batch alone (one the user started). It is also
   kept as a file in images/<date>/ or music/<date>/, and an image of a bridged
   conversation goes to that chat at once.
4. At the end, also after a Stop, an error or a restart's cut: ComfyUI frees its models
   and memory (POST /free), then the lease is released.

"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import httpx

from ..attachments import blob_path, image_size
from ..core.ids import new_id, now
from ..settings.registry import provider_headers
from . import presets as presets_mod, server as server_mod
from .presets import ImageError
from ..core.event_types import IMAGES_BATCH, MUSIC_BATCH

log = logging.getLogger("celine.comfyui")

KINDS = ("images", "music")
EVENTS = {"images": IMAGES_BATCH, "music": MUSIC_BATCH}
SHORT = 8                   # a batch's and an image's short id: the first characters
STOP_WAIT = 15.0            # seconds a Stop waits for the batch to settle
WAIT_MAX = 3600.0           # seconds its call with wait=true holds its turn at most
ANSWER = 5.0                # seconds of its turn kept to answer after such a wait
_EXT = {"image/jpeg": ".jpg", "image/webp": ".webp"}    # a media type's file name end, else .png


def view(b: dict) -> dict:
    """One shape for a batch wherever it appears: the routes, the event, the display
    of the call that started it."""
    return {k: b.get(k) for k in ("id", "kind", "conversation_id", "tool_call_id", "state",
                                  "created_at", "finished_at", "items")}


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["items"] = json.loads(d["items"])
    return d


async def get(store, bid: str) -> dict | None:
    def fn(c: sqlite3.Connection) -> dict | None:
        r = c.execute("SELECT * FROM media_batches WHERE id=?", (bid,)).fetchone()
        return _row(r) if r else None
    return await store.read(fn)


async def find(store, kind: str, ref: str) -> dict:
    """A batch of `kind` by its id or the start of it. Raises ImageError with the
    sentence why not."""
    ref = (ref or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{4,32}", ref):
        raise ImageError(f"{ref or 'An empty id'} is not a batch id: use the {SHORT} "
                         f"characters from its result.")

    def fn(c: sqlite3.Connection) -> list[dict]:
        return [_row(r) for r in c.execute(
            "SELECT * FROM media_batches WHERE kind=? AND substr(id, 1, ?)=? LIMIT 2",
            (kind, len(ref), ref))]
    rows = await store.read(fn)
    if not rows:
        raise ImageError(f"There is no batch {ref}.")
    if len(rows) > 1:
        raise ImageError(f"Several batches start with {ref}: use all {SHORT} characters.")
    return rows[0]


async def list_batches(store, kind: str, conversation: str | None = None,
                       limit: int = 50) -> list[dict]:
    """Newest first; `conversation` keeps one conversation's."""
    def fn(c: sqlite3.Connection) -> list[dict]:
        where, args = "kind=?", [kind]
        if conversation:
            where += " AND conversation_id=?"
            args.append(conversation)
        return [_row(r) for r in c.execute(
            f"SELECT * FROM media_batches WHERE {where} ORDER BY created_at DESC, id DESC "
            "LIMIT ?", (*args, limit))]
    return await store.read(fn)


def _publish(store, c: sqlite3.Connection, b: dict) -> None:
    v = view(b)
    if b["tool_call_id"]:
        c.execute("UPDATE tool_calls SET display=? WHERE id=?",
                  (json.dumps(v, ensure_ascii=False), b["tool_call_id"]))
    if b["conversation_id"]:
        store.event(c, b["conversation_id"], EVENTS[b["kind"]], {"batch": v})
    else:
        store.global_event(c, EVENTS[b["kind"]], {"batch": v})


async def _insert(store, b: dict) -> None:
    def fn(c: sqlite3.Connection) -> None:
        c.execute("INSERT OR REPLACE INTO media_batches(id,kind,conversation_id,"
                  "tool_call_id,state,"
                  "items,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
                  (b["id"], b["kind"], b["conversation_id"], b["tool_call_id"],
                   b["state"], json.dumps(b["items"], ensure_ascii=False),
                   b["created_at"], b["finished_at"]))
        _publish(store, c, b)
    await store.write(fn)


async def _save(store, b: dict) -> None:
    def fn(c: sqlite3.Connection) -> None:
        if c.execute("UPDATE media_batches SET state=?, items=?, finished_at=? WHERE id=?",
                     (b["state"], json.dumps(b["items"], ensure_ascii=False),
                      b["finished_at"], b["id"])).rowcount == 0:
            return                      # gone with its conversation
        _publish(store, c, b)
    await store.write(fn)


async def sweep(store) -> int:
    """At startup: a batch the restart cut is stopped, its unfinished items too."""
    def fn(c: sqlite3.Connection) -> int:
        rows = [_row(r) for r in c.execute(
            "SELECT * FROM media_batches WHERE state='running'")]
        for b in rows:
            _settle(b)
            c.execute("UPDATE media_batches SET state=?, items=?, finished_at=? "
                      "WHERE id=?", (b["state"], json.dumps(b["items"], ensure_ascii=False),
                                     b["finished_at"], b["id"]))
            if b["tool_call_id"]:
                c.execute("UPDATE tool_calls SET display=? WHERE id=?",
                          (json.dumps(view(b), ensure_ascii=False), b["tool_call_id"]))
        return len(rows)
    return await store.write(fn)


def _settle(b: dict) -> None:
    """The end of a batch: what did not finish is stopped."""
    for item in b["items"]:
        if item["state"] in ("waiting", "running"):
            item["state"] = "stopped"
        item.pop("note", None)
    b["state"] = "stopped" if any(i["state"] == "stopped" for i in b["items"]) else "done"
    b["finished_at"] = now()


def keep(cfg, data: bytes, kind: str, name: str) -> Path:
    """Everything made also lands in images/<date>/ or music/<date>/ in the data folder
    (its time zone), a plain folder to browse outside the app."""
    from ..clock import zone
    t = datetime.now(zone(cfg))
    folder = cfg.data_dir / kind / f"{t:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{t:%H-%M-%S}_{name}"
    path.write_bytes(data)
    return path


BATCH_MAX = 16          # slots in one batch: items times count


class TooMany(Exception):
    """A batch past BATCH_MAX: the sentence says so."""


class Batches:
    """Creates, runs and stops batches. The running app holds one (core/services.py),
    with its task group, where every batch runs."""

    def __init__(self, app, tg=None):
        self.app = app                  # core/services.py: cfg, store, llm, bridge
        self.tg = tg
        self.running: dict[str, tuple[anyio.CancelScope, anyio.Event]] = {}

    # ---- create ----------------------------------------------------------------------

    async def create(self, kind: str, items: list[dict], *, count: int = 1,
                     conversation_id: str | None = None,
                     tool_call_id: str | None = None, clip: bool = False) -> dict:
        """The batch with one slot per item (each item `count` times, with its own
        seed), stored. A batch of its call takes a source only from its
        rules. TooMany past BATCH_MAX slots; with `clip` the first BATCH_MAX are made
        instead and the first slot says so."""
        items = items if isinstance(items, list) else [items] if items else []
        count = _int(count) or 1
        n = len(items) * count
        clipped = None
        if n > BATCH_MAX and clip:
            clipped = (f"Only the first {BATCH_MAX} of the {n} images were queued: a "
                       f"batch holds {BATCH_MAX}.")
        elif n > BATCH_MAX:
            unit = "images" if kind == "images" else "tracks"
            raise TooMany(f"A batch holds at most {BATCH_MAX} {unit} (items times count); "
                          f"this one asks for {n}.")
        router = self.app.llm.router
        ordered = await router.order(presets_mod.ROLES[kind])
        slots: list[dict] = []
        for raw in items:
            raw = raw if isinstance(raw, (dict, str)) else {}
            for k in range(count):
                if len(slots) >= BATCH_MAX:
                    break
                slots.append(await self._slot(kind, raw, len(slots), k, ordered,
                                              conversation_id, tool_call_id is not None))
        if clipped and slots:
            slots[0].setdefault("warnings", []).insert(0, clipped)
        b = {"id": new_id(), "kind": kind, "conversation_id": conversation_id,
             "tool_call_id": tool_call_id, "state": "running", "created_at": now(),
             "finished_at": None, "items": slots}
        if not any(i["state"] == "waiting" for i in slots):
            _settle(b)
        await _insert(self.app.store, b)
        return b

    async def _slot(self, kind: str, raw: dict, index: int, k: int, ordered,
                    cid: str | None, hers: bool) -> dict:
        bad: list[str] = []
        if kind == "images":
            raw, bad = tidy(raw)
        elif not isinstance(raw, dict):
            raw = {}
        given = _int(raw.get("seed"), positive=False)
        seed = given + k if given is not None else secrets.randbelow(2 ** 48)
        if kind == "images":
            item = {"index": index, "state": "waiting", "prompt": str(raw.get("prompt") or ""),
                    "seed": seed}
            for key in ("negative", "model", "mode"):
                if raw.get(key):
                    item[key] = str(raw[key])
            if raw.get("loras"):
                item["loras"] = list(raw["loras"])
        else:
            item = {"index": index, "state": "waiting", "style": str(raw.get("style") or ""),
                    "lyrics": str(raw.get("lyrics") or ""),
                    "title": str(raw.get("title") or "track"),
                    "instrumental": raw.get("instrumental") is not False, "seed": seed}
        try:
            if bad:
                raise ImageError(" ".join(bad))
            if kind == "music" and raw.get("duration") is not None:
                d = raw["duration"]
                if isinstance(d, bool) or not isinstance(d, (int, float)) or d <= 0:
                    raise ImageError("The duration is a number of seconds.")
                item["duration"] = d
            if kind == "images":
                await self._image_slot(item, raw, ordered, cid, hers)
            else:
                if not item["style"].strip():
                    raise ImageError("The style is empty.")
                p, pid = self._pick(kind, ordered, None, "music", [])
                item.update(preset=p.id, provider=pid)
                if p.takes("duration"):
                    item.setdefault("duration", p.default("duration"))
        except ImageError as e:
            item.update(state="failed", error=str(e))
        return item

    async def _image_slot(self, item: dict, raw: dict, ordered, cid, hers: bool) -> None:
        """The agent's item as it gave it, checked: the prompt goes to the workflow as it is;
        what does not fit the preset fails the item with the sentence why."""
        if not item["prompt"].strip():
            raise ImageError("The prompt is empty.")
        given = [k for k in presets_mod.SOURCES if raw.get(k)]
        names = presets_mod.SOURCES[:len(given)]
        for key, name in zip(given, names):
            got = await self._source(str(raw[key]), cid, hers)
            item[name] = {k: got[k] for k in ("id", "sha256", "filename", "media_type")}
        source = item.get("source")
        warn = item.setdefault("warnings", []).append
        if list(names) != given:
            warn(f"The images were taken in order: {', '.join(given)} became "
                 f"{', '.join(names)}.")
        task = "edit" if source else "generate"
        wants = ([item["model"]] if item.get("model") else []) + [
            r for r, _ in presets_mod.parse_loras(item.get("loras"))]
        name = str(raw.get("preset") or "").strip() or None
        mode = item.get("mode")

        def fits(q: presets_mod.Preset) -> bool:
            return q.images >= len(given) and (not mode or (
                "mode" in q.parameters and mode in q.parameters["mode"].choices))
        p, pid = self._pick("images", ordered, name, task, wants, fits)
        if len(given) > p.images:
            takes = [q.id for q in presets_mod.of("images").values()
                     if q.images >= len(given)]
            raise ImageError(f"{p.label} takes {p.images} source image"
                             f"{'s' if p.images > 1 else ''}. {len(given)} images: "
                             f"{' or '.join(takes) or 'a preset that takes them'}.")
        if mode and not fits(p):
            choices = list(p.parameters["mode"].choices) if "mode" in p.parameters else []
            raise ImageError(f"{p.label} has no variant {mode!r}" + (
                f": its variants are {', '.join(choices)}." if choices else
                ": it has none."))
        item.update(preset=p.id, provider=pid)
        if item.get("negative") and not p.takes("negative"):
            warn(f"{p.label} reads no negative prompt, so it is not used: say what you "
                 f"want instead.")
        if item.get("model") or item.get("loras"):
            await self._choose_now(item, p, pid)
        shape = None
        if source:
            path = blob_path(self.app.cfg, source["sha256"])
            shape = image_size(path.read_bytes()) if path.exists() else None
        item["width"], item["height"] = p.fit(raw.get("width"), raw.get("height"), shape)
        if not item["warnings"]:
            item.pop("warnings")

    async def _choose_now(self, item: dict, p, pid: str) -> None:
        """The model and LoRAs checked when the batch is made, not when the image
        renders: what is wrong fails now, where it sees it. The ids are stored as
        resolved."""
        from .. import image_catalog
        router = self.app.llm.router
        prov = router.s.providers.get(pid) or {}
        try:
            cat = await image_catalog.load(prov["base_url"],
                                           provider_headers(router.s, pid),
                                           transport=router.transport)
        except (httpx.HTTPError, ValueError) as e:
            raise ImageError(f"ComfyUI did not list its models ({type(e).__name__}).") \
                from None
        choice = presets_mod.choose(p, cat, model=item.get("model"),
                                    loras=item.get("loras"))
        item.pop("model", None)
        item.pop("loras", None)
        item.update({k: v for k, v in choice.recipe().items() if k != "triggers"})

    async def _source(self, ref: str, cid: str | None, hers: bool) -> dict:
        """The image an edit starts from: its portrait (portrait, or
        name>, or its name alone); else its own: one it made (an attachment of its own
        message, in any conversation) or one the user attached to this conversation,
        by its id or the start of it. The user's: any image attachment, by its id."""
        from .. import attachments
        if face := self._portrait(ref):
            return face
        store = self.app.store
        wanted = ref.strip().lower()
        if not hers:
            row = await attachments.get(store, wanted)
            if row is None or row["kind"] != "image":
                raise ImageError(f"There is no image {wanted}.")
            return row
        row = await attachments.her_image(store, wanted, cid, self.app.cfg.user)
        if isinstance(row, str):
            raise ImageError(row)
        return row

    def _portrait(self, ref: str) -> dict | None:
        """`portrait`, `portrait:<its name>`, or its name alone: its portrait as an
        edit's source; None for another ref. Raises ImageError when it has none."""
        from .. import her
        cfg = self.app.cfg
        key = ref.strip().casefold()
        if key not in ("portrait", f"portrait:{cfg.name.casefold()}", cfg.name.casefold()):
            return None
        got = her.portrait_file(cfg)
        if got is None:
            raise ImageError(f"{cfg.name} has no portrait.")
        return {"id": "portrait", "sha256": cfg.her.portrait,
                "filename": f"portrait{_EXT.get(got[1], '.png')}", "media_type": got[1]}

    def _pick(self, kind: str, ordered, name: str | None, task: str,
              wants: list[str], fits=None) -> tuple[presets_mod.Preset, str]:
        """The preset asked for (by id or label), or the first of the role for the task
        that can answer: one that `fits` the item (its images, its variant) and whose
        catalog has the model or LoRA asked for, else the first. -> (preset, provider
        id)."""
        s = self.app.llm.router.s
        found = presets_mod.of(kind)

        def of_task(ref: str) -> bool:
            p = found.get(s.split(ref)[1])
            return p is not None and p.task == task

        chain = [r for r in ordered.chain if of_task(r)]
        refs = [r for r in ordered.refs if of_task(r)]
        what = {"generate": "new images", "edit": "image edits", "music": "music"}[task]
        if not chain:
            raise ImageError(f"No preset for {what} is set. {self.app.cfg.user} chooses "
                             f"presets in Settings, Images & Music.")
        if not name:
            if not refs:
                raise ImageError(" ".join(dict.fromkeys(
                    why for ref, why in ordered.skipped if ref in chain))
                    or "No preset can answer now.")
            if fits:
                refs = [r for r in refs if fits(found[s.split(r)[1]])] or refs
            if wants:
                refs = [r for r in refs if any(presets_mod.owns(found[s.split(r)[1]], w)
                                               for w in wants)] or refs
            ref = refs[0]
        else:
            want = str(name).strip().lower()
            ref = next((r for r in chain if want in (
                s.split(r)[1].lower(), str((s.model(r) or {}).get("label", "")).lower())),
                None)
            if ref is None:
                names = ", ".join(dict.fromkeys(s.split(r)[1] for r in chain))
                raise ImageError(f"There is no preset {name!r} for {what}. The presets "
                                 f"are: {names}.")
            if ref not in refs:
                raise ImageError(dict(ordered.skipped).get(ref)
                                 or f"{found[s.split(ref)[1]].label} cannot answer now.")
        pid, pname = s.split(ref)
        return found[pname], pid

    # ---- run -------------------------------------------------------------------------

    def start(self, bid: str) -> None:
        """The batch runs on in the app's task group; `wait` and `stop` reach it at
        once."""
        self.running[bid] = (anyio.CancelScope(), anyio.Event())
        self.tg.start_soon(self.run, bid)

    async def wait(self, bid: str) -> None:
        """Until the batch has settled; at once when it does not run."""
        hit = self.running.get(bid)
        if hit is not None:
            await hit[1].wait()

    async def begin(self, b: dict, *, wait: bool, time_left: float | None) -> dict:
        """The agent's call's batch, started; with `wait`, waited for as long as its turn's time
        allows, ANSWER seconds kept to answer. -> the batch as it is then."""
        if b["state"] != "running":
            return b
        self.start(b["id"])
        if wait:
            limit = WAIT_MAX if time_left is None else min(WAIT_MAX, time_left - ANSWER)
            with anyio.move_on_after(max(0.0, limit)):
                await self.wait(b["id"])
            b = await get(self.app.store, b["id"])
        return b

    async def run(self, bid: str) -> dict:
        """Renders what waits (the module docstring) and returns the settled batch."""
        store = self.app.store
        scope, ended = self.running.setdefault(bid, (anyio.CancelScope(), anyio.Event()))
        b = None
        try:
            b = await get(store, bid)
            if b is None or b["state"] != "running":
                return b
            with scope:
                async with contextlib.AsyncExitStack() as stack:
                    servers = _Servers(self, b, stack, await self._turn_of(b))
                    for item in b["items"]:
                        if item["state"] == "waiting":
                            await self._one(b, item, servers)
        finally:
            self.running.pop(bid, None)
            with anyio.CancelScope(shield=True):
                if b is not None and b["state"] == "running":
                    _settle(b)
                    await _save(store, b)
                ended.set()
        return b

    async def retry(self, bid: str, index: int) -> dict:
        """Makes one failed or stopped item again, as it was asked (its prompt, preset,
        size and seed), together with every other failed or stopped item of the batch,
        and runs the batch for them. ImageError with the sentence why not:
        the batch is still running, the item is done, or it never reached a preset (its
        own error says why). -> the batch as it is then."""
        b = await get(self.app.store, bid)
        if b is None:
            raise ImageError("There is no such batch.")
        if b["state"] == "running" or bid in self.running:
            raise ImageError("The batch is still running.")
        item = next((i for i in b["items"] if i["index"] == index), None)
        if item is None or item["state"] not in ("failed", "stopped"):
            raise ImageError("Only a failed or stopped item can be made again.")
        if "preset" not in item:
            raise ImageError(item.get("error") or "This item cannot be made again.")
        # one Retry makes every other failed or stopped item again too (a ComfyUI crash
        # usually takes the whole rest of the batch with it)
        for it in b["items"]:
            if it is not item and (it["state"] not in ("failed", "stopped")
                                   or "preset" not in it):
                continue
            for k in ("error", "seconds", "note", "attachment"):
                it.pop(k, None)
            it["state"] = "waiting"
        b.update(state="running", finished_at=None)
        # held as running before the first await: a second Retry at once is refused
        self.running[bid] = (anyio.CancelScope(), anyio.Event())
        try:
            await _save(self.app.store, b)
        except BaseException:
            self.running.pop(bid, None)
            # the runner never started: a batch left "running" could not be stopped or retried
            _settle(b)
            with anyio.CancelScope(shield=True):
                await _save(self.app.store, b)
            raise
        self.tg.start_soon(self.run, bid)
        return b

    async def stop(self, bid: str) -> bool:
        """Stops a running batch: the item in ComfyUI is interrupted, the rest are not
        made. Waits until it has settled. False when it was not running."""
        hit = self.running.get(bid)
        if hit is None:
            return False
        hit[0].cancel()
        with anyio.move_on_after(STOP_WAIT):
            await hit[1].wait()
        return True

    async def _turn_of(self, b: dict) -> str | None:
        """The agent's turn that started the batch: the GPU wait is credited to its clock."""
        if not b["tool_call_id"]:
            return None
        return await self.app.store.read(lambda c: (c.execute(
            "SELECT m.turn_id FROM tool_calls tc JOIN messages m ON m.id = tc.message_id "
            "WHERE tc.id=?", (b["tool_call_id"],)).fetchone() or [None])[0])

    async def _one(self, b: dict, item: dict, servers: "_Servers") -> None:
        store = self.app.store
        item["state"] = "running"
        await _save(store, b)

        async def note(line: str, **_) -> None:
            if item.get("note") != line:
                item["note"] = line
                await _save(store, b)

        started = time.monotonic()
        router = self.app.llm.router
        try:
            if b["kind"] == "images":
                await self._image(b, item, servers, note)
            else:
                await self._song(b, item, servers, note)
            item["state"] = "done"
            router.note_ok(f"{item['provider']}/{item['preset']}")
        except ImageError as e:
            item.update(state="failed", error=str(e))
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            sentence = router._unreachable_sentence(item["provider"], e)
            router.note_unreachable(item["provider"], sentence)
            item.update(state="failed", error=sentence)
        except httpx.HTTPError as e:
            item.update(state="failed", error=f"ComfyUI stopped answering "
                                              f"({type(e).__name__}).")
        except Exception as e:              # one item, never the batch
            log.exception("item failed", extra={"event": "comfyui.item_failed",
                                                "batch": b["id"]})
            item.update(state="failed", error=f"It failed: {type(e).__name__}: {e}")
        item["seconds"] = round(time.monotonic() - started, 1)
        item.pop("note", None)
        await _save(store, b)

    def _preset(self, kind: str, item: dict) -> presets_mod.Preset:
        p = presets_mod.of(kind).get(item["preset"])
        if p is None:
            raise ImageError(f"The preset {item['preset']} is gone from Settings, "
                             f"Images & Music.")
        return p

    async def _image(self, b: dict, item: dict, servers: "_Servers", note) -> None:
        p = self._preset("images", item)
        comfy, timeout = await servers.get(item["provider"], note)
        await servers.check(item["provider"], p)
        values: dict[str, Any] = {"prompt": item["prompt"], "seed": item["seed"],
                                  "width": item["width"], "height": item["height"]}
        if item.get("negative") and p.takes("negative"):
            values["negative"] = item["negative"]
        if item.get("mode"):
            values["mode"] = item["mode"]
        choice = presets_mod.Choice()
        if item.get("model") or item.get("loras"):
            choice = presets_mod.choose(p, await servers.catalog(item["provider"]),
                                        model=item.get("model"), loras=item.get("loras"))
        sources = [(name, item[key]) for key, name in
                   zip(presets_mod.SOURCES, presets_mod.IMAGES) if item.get(key)]
        if sources:
            await note("Sending the source image" + ("s" if len(sources) > 1 else "")
                       + " to ComfyUI")
        for name, src in sources:
            path = blob_path(self.app.cfg, src["sha256"])
            if not path.exists():
                raise ImageError(f"The file of {src['filename']} is no longer stored.")
            values[name] = await comfy.upload(
                path.read_bytes(), f"celine-{uuid4().hex[:16]}"
                f"{_EXT.get(src['media_type'], '.png')}", src["media_type"])
        graph = p.build(values)
        for name in presets_mod.IMAGES[1:]:     # an image not given: its node goes
            if p.takes(name) and name not in values:
                graph = presets_mod.unwire(graph, p.parameters[name].inputs[0][0])
        graph = presets_mod.attach(graph, p, choice)
        data, media_type = await server_mod.run(comfy, graph, note, timeout)
        w, h = image_size(data) or (item["width"], item["height"])
        ext = _EXT.get(media_type, ".png")
        row = await self._keep(b, data, f"{p.id}-{item['seed']}{ext}", media_type)
        item["attachment"] = {**row, "width": w, "height": h}
        item.update(choice.recipe())
        bridge = self.app.bridge
        if b["conversation_id"] and bridge is not None and await bridge.send_image(
                b["conversation_id"], data, row["filename"], media_type):
            item["bridged"] = True

    async def _song(self, b: dict, item: dict, servers: "_Servers", note) -> None:
        p = self._preset("music", item)
        comfy, timeout = await servers.get(item["provider"], note)
        await servers.check(item["provider"], p)
        values = {"style": item["style"], "lyrics": item["lyrics"], "seed": item["seed"],
                  "instrumental": item["instrumental"]}
        if "duration" in item:              # a preset without one leaves it out
            values["duration"] = float(item["duration"])
        data, media_type = await server_mod.run(comfy, p.build(values), note, timeout)
        slug = re.sub(r"[^a-z0-9]+", "_", item["title"].lower()).strip("_")[:32] or "track"
        ext = ".mp3" if "mpeg" in media_type or "mp3" in media_type else ".flac"
        item["attachment"] = await self._keep(b, data, f"{slug}-{item['seed']}{ext}",
                                              media_type)

    async def _keep(self, b: dict, data: bytes, name: str, media_type: str) -> dict:
        from .. import attachments
        cfg = self.app.cfg
        row = await attachments.save_generated(cfg, self.app.store, data, name, media_type,
                                               b["tool_call_id"], batch_id=b["id"])
        keep(cfg, data, b["kind"], name)
        return {k: row[k] for k in ("id", "sha256", "filename", "media_type", "bytes")}


class _Servers:
    """The ComfyUI servers one batch uses: a client each, the lease of its GPU taken
    before its first item (once per GPU, which several servers may share) and held to
    the end of the batch, its catalog. A GPU whose lease timed out fails every later
    item on it at once. At the end each server frees its models (bounded, also after a
    Stop), then the leases are released and the clients closed."""

    def __init__(self, batches: Batches, b: dict, stack: contextlib.AsyncExitStack,
                 turn_id: str | None):
        self.app = batches.app
        self.b = b
        self.stack = stack
        self.turn_id = turn_id
        self.clients: dict[str, tuple[server_mod.Comfy, float]] = {}
        self.catalogs: dict[str, Any] = {}
        self.info: dict[str, dict] = {}          # provider -> its /object_info entries so far
        self.gpus: dict[str, str | None] = {}   # leased GPU -> None, or why it is not

    async def get(self, pid: str, note) -> tuple[server_mod.Comfy, float]:
        if pid in self.clients:
            return self.clients[pid]
        from ..backends.queue import QueueTimeout
        router = self.app.llm.router
        s = router.s
        p = s.providers.get(pid)
        if p is None:
            raise ImageError(f"The provider {pid} is gone from Settings.")
        opts = p.get("options") or {}
        shared = opts.get("shares_gpu_with") or None
        if self.gpus.get(shared):
            raise ImageError(self.gpus[shared])
        comfy = server_mod.Comfy(p["base_url"], provider_headers(s, pid), router.transport)
        self.stack.push_async_callback(_shielded, comfy.close)
        if shared and shared not in self.gpus:
            async def waiting(_pos) -> None:
                await note("Waiting for the GPU")
            try:
                await self.stack.enter_async_context(self.app.llm.gpu_lease(
                    shared, label=self.b["kind"], conversation_id=self.b["conversation_id"],
                    turn_id=self.turn_id, on_wait=waiting))
            except QueueTimeout:
                label = (s.providers.get(shared) or {}).get("label", shared)
                self.gpus[shared] = f"The GPU stayed busy with {label} requests."
                raise ImageError(self.gpus[shared]) from None
            self.gpus[shared] = None
        self.stack.push_async_callback(_free, comfy)
        self.clients[pid] = (comfy, float(opts.get("timeout") or 600.0))
        return self.clients[pid]

    async def check(self, pid: str, p: presets_mod.Preset) -> None:
        """Before a prompt is queued: every node and every file the preset's workflow
        names is on the server, else ImageError saying which. Asked once per node class
        per batch."""
        comfy, _ = self.clients[pid]
        info = self.info.setdefault(pid, {})
        want = {n["class_type"] for n in p.workflow.values()} - set(info)
        if want:
            info.update(await comfy.object_info(want))
        why = p.missing(info)
        if why:
            raise ImageError(why)

    async def catalog(self, pid: str):
        if pid not in self.catalogs:
            from .. import image_catalog
            router = self.app.llm.router
            p = router.s.providers[pid]
            try:
                self.catalogs[pid] = await image_catalog.load(
                    p["base_url"], provider_headers(router.s, pid),
                    transport=router.transport)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                raise
            except (httpx.HTTPError, ValueError) as e:
                raise ImageError(f"ComfyUI did not list its models ({type(e).__name__}).") \
                    from None
        return self.catalogs[pid]


async def _shielded(fn) -> None:
    with anyio.CancelScope(shield=True), anyio.move_on_after(server_mod.CLEANUP_SECONDS):
        await fn()


async def _free(comfy: server_mod.Comfy) -> None:
    try:
        await _shielded(comfy.free)
    except httpx.HTTPError as e:
        log.info("comfyui free failed", extra={"event": "comfyui.free_failed",
                                               "error": type(e).__name__})


# ---- its items, tidied ---------------------------------------------------------------

ITEM_KEYS = ("prompt", *presets_mod.SOURCES, "preset", "negative", "width", "height", "seed",
             "model", "loras", "variant")


def _int(v: Any, positive: bool = True) -> int | None:
    """A whole number from an int, a float or a digit string; None otherwise."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+(\.0+)?\s*", v):
        v = float(v)
    if isinstance(v, (int, float)) and (v > 0 or not positive):
        return int(v)
    return None


def tidy(raw: dict | str) -> tuple[dict, list[str]]:
    """The agent's item as the batch reads it, and what is wrong with it, in sentences: an
    unknown field, a number that is none, loras that are no list. A string is the
    prompt. The texts are kept as it wrote them; `variant` is the preset's mode."""
    if isinstance(raw, str):
        return {"prompt": raw}, []
    out: dict[str, Any] = {}
    bad: list[str] = []
    for key, v in raw.items():
        if key in ITEM_KEYS:
            out[key] = v
        elif v not in (None, "", [], {}):
            bad.append(f"Field '{key}' is not an item field (fields: "
                       f"{', '.join(ITEM_KEYS)}).")
    for k in ("width", "height", "seed"):
        if k in out:
            n = _int(out[k], positive=k != "seed")
            if n is None and out[k] not in (None, "", 0):
                bad.append(f"{k} {out[k]!r} is not a number.")
            out[k] = n
    v = out.get("loras")
    if isinstance(v, list):
        out["loras"] = [str(x) for x in v]
    elif v not in (None, "", [], {}):
        bad.append("loras is a list of LoRA ids.")
    for k in ("prompt", "negative", *presets_mod.SOURCES, "preset", "model", "variant"):
        if k in out and out[k] is not None and not isinstance(out[k], str):
            out[k] = str(out[k])
    if "variant" in out:
        out["mode"] = out.pop("variant")
    return out, bad
