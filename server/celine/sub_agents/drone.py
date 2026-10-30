"""Drones: sub-agents whose briefs opencode works on in `opencode serve` on a device
(sub_agents__start with `device`), close to the machine, while she supervises.

A device is a plugin that is on with a [drones] block (celine/devices.py). The one
opencode client is the box's, shellbox/agents/opencode.py, loaded from here.

A drone's turn: the messages it got since the last one sent (`agent_sent`) go to its
opencode session (`agent_session`, made at its first turn and kept, so the same name
resumes the same session and a restart finds it again) with `prompt_async`, on the
one model its conversation was started and approved on (skill.py _model), which opencode reaches through the app's model proxy
(queue, freeze and usage stay in the app). The models the proxy offers are put into
the device's opencode config first when they differ (PATCH /global/config), and only
while no session there works: a changed config disposes every opencode instance.
The messages are marked sent before they go: a crash in between loses them rather than
sending them twice (at most once); a refusal or no connection unmarks them, a send
that got no answer (a timeout after it went out) keeps them marked. Its progress comes
from opencode's event stream (GET /event): one line of the reply's thinking per tool it starts, and the tool in
`agent_step` (the "Now:" of read and the overview); a stream that breaks says so in a
line of its own and is opened again. The turn ends when the session is idle: the reply is opencode's last answer
as it came; the tokens of its answers since the last one counted (`agent_seen`) are the
turn's usage, the files the messages of the turn changed (opencode's diff) go into the
reply's raw response, where the report finds them (report.work). A waking message that
arrives while it works ends the turn without stopping opencode: the next turn hands it
to the session as it works (opencode takes a prompt into the work it is on). A turn
that finds no new message follows the session while it works, else takes what it
answered since the last count. A Stop or the end of the turn's time aborts the session.
"""
from __future__ import annotations

import importlib.util
import logging
import queue
import sqlite3
import threading

import anyio

from ..backends.base import BackendError, Done, ReasoningDelta, TextDelta, Usage
from ..config import ROOT
from ..devices import devices
from ..core.ids import now
from ..settings import CONVERSATION_MODELS, her_models

log = logging.getLogger("celine.drones")

CLIENT = ROOT / "shellbox" / "agents" / "opencode.py"
POLL = 5.0          # seconds between looks for a waking message and at the session
READ = 120.0        # seconds of a quiet event stream before it is opened again
RETRY = 5.0         # seconds before a lost event stream is opened again
_client = None


def client():
    """shellbox/agents/opencode.py, loaded once."""
    global _client
    if _client is None:
        spec = importlib.util.spec_from_file_location("celine_opencode_client", CLIENT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _client = mod
    return _client


def _row(c: sqlite3.Connection, cid: str) -> sqlite3.Row:
    return c.execute("SELECT agent_device, agent_session, agent_sent, agent_seen "
                     "FROM conversations WHERE id=?", (cid,)).fetchone()


def unsent(c: sqlite3.Connection, cid: str, sent: int | None) -> tuple[str, int | None]:
    """The messages it got since the last one sent -> (their text, the last one's rowid)."""
    rows = c.execute("SELECT rowid, content FROM messages WHERE conversation_id=? AND "
                     "role='user' AND rowid > ? ORDER BY rowid", (cid, sent or 0)).fetchall()
    text = "\n\n".join(str(r["content"] or "") for r in rows).strip()
    return text, (rows[-1]["rowid"] if rows else None)


def waking(c: sqlite3.Connection, cid: str) -> bool:
    """A message due in `cid` that wakes it (intercom/deliver.py)."""
    return c.execute(
        "SELECT 1 FROM intercom_deliveries d JOIN intercom i ON i.id = d.intercom_id "
        "WHERE d.conversation_id=? AND d.state='pending' AND i.wake=1 AND "
        "i.cancelled_at IS NULL AND (i.due_at IS NULL OR i.due_at <= ?) LIMIT 1",
        (cid, now())).fetchone() is not None


class Unseen(Exception):
    """The answer counted last is no longer in the session."""


def tally(msgs: list[dict], seen: str | None) -> dict:
    """Its answers after `seen` that are complete: {text: the last one's text, models,
    usage, seen: the last one's id, asked: the ids of the messages sent after `seen`}.
    Raises Unseen when `seen` is not among them."""
    after = msgs
    ids = [m["info"].get("id") for m in msgs]
    if seen is not None:
        if seen not in ids:
            raise Unseen(seen)
        after = msgs[ids.index(seen) + 1:]
    out = {"text": "", "models": [], "n": [0, 0, 0, 0], "seen": seen, "asked": []}
    for m in after:
        info = m["info"]
        if info.get("role") == "user":
            out["asked"].append(info.get("id"))
            continue
        if info.get("role") != "assistant" or not (info.get("time") or {}).get("completed"):
            continue
        t = info.get("tokens") or {}
        cache = t.get("cache") or {}
        read, write = int(cache.get("read") or 0), int(cache.get("write") or 0)
        n = out["n"]
        n[0] += int(t.get("input") or 0) + read + write
        n[1] += int(t.get("output") or 0) + int(t.get("reasoning") or 0)
        n[2] += read
        n[3] += write
        if info.get("modelID") and info["modelID"] not in out["models"]:
            out["models"].append(info["modelID"])
        texts = [p["text"] for p in m.get("parts") or []
                 if p.get("type") == "text" and p.get("text")]
        if texts:
            out["text"] = texts[-1]
        out["seen"] = info.get("id")
    out["usage"] = Usage(*out.pop("n"))
    return out


def session_of(ev: dict) -> str | None:
    """The session an event is about: in its properties, or in its part's."""
    props = ev.get("properties") or {}
    return props.get("sessionID") or (props.get("part") or {}).get("sessionID")


def step_line(part: dict) -> str:
    """One line for a tool it started: its name and opencode's title, else its input."""
    state = part.get("state") or {}
    what = state.get("title") or " ".join(
        str(v) for v in (state.get("input") or {}).values() if isinstance(v, str))
    what = " ".join(what.split())
    return f"{part.get('tool')}: {what[:200]}" if what else str(part.get("tool"))


class DroneBackend:
    supports_tools = False
    supports_images = False

    def __init__(self, services, cid: str, cli: str, device: str, clock=None):
        self.services, self.cid, self.cli, self.device = services, cid, cli, device
        self.clock = clock
        # the one model it was started and approved on (skill.py _model)
        self.model = CONVERSATION_MODELS.get(cid)
        self.name = f"{cli}:{device}"
        self.request_sent: dict | None = None

    async def _set(self, **cols) -> None:
        keys = ", ".join(f"{k}=?" for k in cols)
        await self.services.store.write(lambda c: c.execute(
            f"UPDATE conversations SET {keys} WHERE id=?", (*cols.values(), self.cid)))

    async def _step(self, text: str | None) -> None:
        from .skill import announce
        await self._set(agent_step=text)
        await announce(self.services, self.cid)

    async def _models(self, oc, server, dev) -> None:
        """The proxy's models into the device's opencode config, when they differ and no
        session there works (a changed config disposes every instance). While one
        works, the config stays: refused when it lacks this drone's model."""
        from ..api.llm_proxy import listing
        s = self.services.cfg.settings
        want = {ref: {"name": ref, "limit": limit} for ref, limit in oc._limits(
            listing(s, her_models.allowed(s)), log.info).items()}
        have = (((await _run(oc._call, "GET", "/global/config", server=server) or {})
                 .get("provider") or {}).get("celine") or {}).get("models") or {}
        if all(have.get(k) == v for k, v in want.items()):
            return
        working = [k for k, v in (await _run(oc._call, "GET", "/session/status",
                                             server=server) or {}).items()
                   if (v or {}).get("type") in ("busy", "retry")]
        if not working:
            await _run(oc._call, "PATCH", "/global/config",
                       {"provider": {"celine": {"models": want}}}, server=server)
        elif self.model not in have:
            raise BackendError(
                f"{self.model} is not in the opencode config on {dev.label} yet, and it "
                f"changes only while no drone works there ({len(working)} working now); "
                f"start this one when they are done.", retryable=False)

    async def stream(self, messages, tools, body=None):
        oc = client()
        dev = devices(self.services.cfg).get(self.device)
        if dev is None:
            raise BackendError(f"The device {self.device} is not on.", retryable=False)
        if dev.why:
            raise BackendError(dev.why, retryable=False)
        if self.model is None:
            raise BackendError("This drone has no model to run on.", retryable=False)
        server = oc.Server(dev.url, dev.password, f"{dev.log} on {dev.label}")
        store = self.services.store
        row = await store.read(lambda c: _row(c, self.cid))
        brief, last = await store.read(lambda c: unsent(c, self.cid, row["agent_sent"]))
        minutes = float(self.services.cfg.r("sub_agents.cli_minutes"))
        if self.clock is not None:          # the drone's minutes are its own, not the turn's
            self.clock.deadline = max(self.clock.deadline,
                                      anyio.current_time() + minutes * 60 + 300)
        ended = False
        session = row["agent_session"]
        try:
            try:
                await self._models(oc, server, dev)
                if session is None:
                    if not brief:
                        raise BackendError("There is no brief to work on.", retryable=False)
                    session = await _run(oc.new_session, brief, "", server)
                    await self._set(agent_session=session)
                busy = await _run(oc._busy_one, session, server)
            except oc.Problem as e:
                raise BackendError(f"{dev.label}: {e}", retryable=False) from None
            events = _Events(oc, server, session)
            try:
                if brief:
                    # marked first: a crash before the send loses it, never sends it twice
                    await self._set(agent_sent=last)
                    try:
                        await _run(oc.prompt, session, self.model, brief, "", server)
                    except oc.Problem as e:
                        if e.case != "unknown":     # surely not taken: again next turn
                            await self._set(agent_sent=row["agent_sent"])
                        raise BackendError(f"{dev.label}: {e}", retryable=False) from None
                    busy = True
                yield ReasoningDelta(f"{self.cli} on {dev.label}, session {session}\n")
                cut, started = False, set()
                while busy:
                    ev = await events.next(POLL)
                    if ev is None:
                        if await store.read(lambda c: waking(c, self.cid)):
                            cut = True
                            break
                        try:
                            busy = await _run(oc._busy_one, session, server)
                        except oc.Problem as e:
                            raise BackendError(f"{dev.label}: {e}", retryable=False) from None
                        continue
                    kind, props = ev.get("type"), ev.get("properties") or {}
                    if kind == LOST:
                        yield ReasoningDelta(f"The event stream from {dev.label} broke "
                                             f"({props['error']}); it is opened again.\n")
                    elif kind == "message.part.updated":
                        part = props.get("part") or {}
                        if part.get("type") == "tool" and part.get("callID") not in started \
                                and (part.get("state") or {}).get("status") == "running":
                            started.add(part.get("callID"))
                            line = step_line(part)
                            await self._step(line)
                            yield ReasoningDelta(line + "\n")
                    elif kind == "session.idle" or (kind == "session.status" and (
                            props.get("status") or {}).get("type") == "idle"):
                        busy = False
            finally:
                await events.close()
            done = await self._done(oc, server, session, row["agent_seen"], cut, dev)
            ended = True
        finally:
            if not ended:
                with anyio.CancelScope(shield=True), anyio.move_on_after(30):
                    try:
                        if session is not None:
                            await _run(oc.abort, session, server)
                    except Exception:   # noqa: BLE001 - the device is gone: nothing runs
                        pass
            with anyio.CancelScope(shield=True):
                await self._step(None)
        if done[0]:
            yield TextDelta(done[0])
        yield done[1]

    async def _done(self, oc, server, session: str, seen: str | None, cut: bool, dev):
        """-> (the reply, Done) of the turn: its answers since `seen`, counted."""
        try:
            msgs = await _run(oc._messages, session, server, None)
            t = tally(msgs, seen)
        except Unseen:
            raise BackendError(
                f"The answer counted last ({seen}) is no longer in session {session} on "
                f"{dev.label}, so what came after it cannot be counted.",
                retryable=False) from None
        except oc.Problem as e:
            raise BackendError(f"{dev.label}: {e}", retryable=False) from None
        try:
            files = {}
            for mid in t["asked"]:
                for d in await _run(oc._call, "GET", f"/session/{session}/diff",
                                    query=f"messageID={mid}", server=server) or []:
                    if isinstance(d, dict) and d.get("path"):
                        files[d["path"]] = d.get("status")
            st = None if cut else await _run(oc.status, session, server)
        except oc.Problem as e:
            raise BackendError(f"{dev.label}: {e}", retryable=False) from None
        await self._set(agent_seen=t["seen"])
        self.request_sent = {"provider": ", ".join(t["models"]) or self.model,
                             "model": self.model,
                             "body": {"device": self.device, "session": session}}
        if st is not None and st["state"] == "failed":
            raise BackendError(f"{self.cli} on {dev.label}: {st['error']}", retryable=False)
        raw = {"device": self.device, "session": session, "files": sorted(files),
               "messages": [m["info"] for m in msgs if m["info"].get("id") in
                            {*t["asked"], t["seen"]}]}
        return t["text"], Done(reason="stop", usage=t["usage"], raw=raw)


async def _run(fn, *args, **kw):
    return await anyio.to_thread.run_sync(lambda: fn(*args, **kw), abandon_on_cancel=True)


LOST = "celine.lost"     # what the pump puts in the queue when the stream broke


class _Events:
    """The session's events from opencode's stream, read in a thread of their own that
    opens the stream again after READ quiet seconds or when it broke (a LOST event says
    why); it ends with close()."""

    def __init__(self, oc, server, session: str):
        self.q: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.end = None
        self.oc, self.server, self.session = oc, server, session
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        while not self.stop.is_set():
            try:
                self.end, each = self.oc.events(self.server, READ)
                if self.stop.is_set():
                    self.end()
                    return
                for ev in each:
                    if self.stop.is_set():
                        return
                    if session_of(ev) == self.session:
                        self.q.put(ev)
                why = "it ended"
            except TimeoutError:
                log.info("drone events quiet, opened again",
                         extra={"event": "drone.events_quiet", "session": self.session})
                continue
            except Exception as e:      # noqa: BLE001 - said in the turn and the log
                why = str(e) or type(e).__name__
            if self.stop.is_set():
                return
            log.warning("drone events lost", extra={"event": "drone.events_lost",
                                                    "session": self.session, "error": why})
            self.q.put({"type": LOST, "properties": {"error": why}})
            self.stop.wait(RETRY)

    async def next(self, timeout: float) -> dict | None:
        """The next event, None after `timeout` seconds without one."""
        try:
            return await _run(self.q.get, True, timeout)
        except queue.Empty:
            return None

    async def close(self) -> None:
        """Ends the stream and waits for the thread."""
        self.stop.set()
        if self.end is not None:
            self.end()
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(self.thread.join, READ + 5)
