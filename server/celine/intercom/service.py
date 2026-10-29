"""Sending, cancelling, and the timer that delivers what comes due."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

import anyio

from ..core.ids import now
from ..missions import store as missions
from ..plans import store as plans
from . import deliver
from .store import insert, read
from ..core.event_types import INTERCOM_STATE

log = logging.getLogger("celine.intercom")


async def send(store, from_addr: str, to: str, text: str, *, kind: str = "message",
               subject: str | None = None, data: dict | None = None, wake: bool = True,
               due_at: str | None = None, until_at: str | None = None,
               reply_to: int | None = None, created_at: str | None = None,
               attachments: list[str] | None = None, only_if=None,
               turn: str | None = None) -> dict | None:
    """One message, in one write with its delivery to every idle recipient.
    -> {intercom_id, message_id, turn_id, seq}: the message and the turn of the first
    conversation it was delivered to at once, else None for those three.
    `attachments`: draft ids, bound to the message when it is delivered. `only_if(c)`:
    checked in the same write; when it is false nothing is sent and this is None.
    The user's reply marks what waits for them in that thread read. `turn`: the
    sender's turn, so a message without reply_to still answers in its thread
    (store.follows).
    KeyError: no such conversation. ValueError: a bad address or attachment."""
    data = dict(data or {})
    if attachments:
        data["attachments"] = list(attachments)

    def fn(c: sqlite3.Connection) -> dict | None:
        if only_if is not None and not only_if(c):
            return None
        for aid in attachments or []:
            if c.execute("SELECT 1 FROM attachments WHERE id=? AND message_id IS NULL AND "
                         "tool_call_id IS NULL AND state='ready'", (aid,)).fetchone() is None:
                raise ValueError(f"attachment {aid} is not ready or already sent")
        iid = insert(c, from_addr, to, kind, text, subject=subject, data=data, wake=wake,
                     due_at=due_at, until_at=until_at, reply_to=reply_to,
                     created_at=created_at, store=store, turn=turn)
        out = {"intercom_id": iid, "message_id": None, "turn_id": None, "seq": None,
               "conversations": [], "resumed": []}
        for (cid,) in c.execute("SELECT conversation_id FROM intercom_deliveries WHERE "
                                "intercom_id=? AND conversation_id IS NOT NULL",
                                (iid,)).fetchall():
            out["conversations"].append(cid)
            if from_addr == "user":
                plans.answered_sync(c, store, cid)
            # the user's reply to its message resumes a stopped mission
            if from_addr == "user" and reply_to is not None and \
                    (row := missions.set_status_sync(c, store, cid, "active", None, "stopped")):
                out["resumed"].append(row)
            r = deliver.deliver_idle_sync(c, store, cid)
            if r and out["message_id"] is None and iid in r["messages"]:
                out.update(message_id=r["messages"][iid], turn_id=r["turn_id"], seq=r["seq"])
        _changed(c, store, iid, out.pop("conversations"))
        if from_addr == "user" and reply_to is not None:
            for rid in read(c, [r[0] for r in c.execute(
                    "SELECT id FROM intercom WHERE thread = (SELECT thread FROM intercom "
                    "WHERE id=?)", (iid,))]):
                _changed(c, store, rid, [])
        return out

    return await store.write(fn)


def _changed(c: sqlite3.Connection, store, iid: int, cids: list[str]) -> None:
    """Inside a write: every open page hears it (the global feed)."""
    for cid in cids or [None]:
        store.global_event(c, INTERCOM_STATE, {"intercom_id": iid,
                                                 "conversation_id": cid})


class Intercom:
    """The app's intercom: `send` as above, plus the dispatcher's notify for the turn it
    queues, and the timer for messages with a due time."""

    def __init__(self, store, notify=None):
        self.store = store
        self.notify = notify or (lambda: None)
        self._wake = anyio.Event()
        self._sent = anyio.Event()      # set and replaced after every send

    async def send(self, from_addr: str, to: str, text: str, **kw) -> dict | None:
        out = await send(self.store, from_addr, to, text, **kw)
        if out is None:
            return None
        for row in out.pop("resumed"):
            await self._time_limit(row)
        if out["turn_id"]:
            self.notify()
        if kw.get("due_at"):
            self._wake.set()
        self._sent.set()
        self._sent = anyio.Event()
        return out

    async def _time_limit(self, row: dict) -> None:
        """A resumed mission's time limit, when it is still ahead: its notice again
        (stopping cancelled it)."""
        from ..missions.store import book_time_limit
        if not row["until"] or row["until"] <= now():
            return
        cid = row["conversation_id"]
        wake = await book_time_limit(self, cid, datetime.fromisoformat(row["until"]))
        await missions.update(self.store, cid, {"wake_id": wake})

    async def wait_for(self, check, timeout: float):
        """`await check()` now and after every message sent, until it gives something
        other than None or `timeout` seconds pass (then None)."""
        with anyio.move_on_after(timeout):
            while True:
                sent = self._sent
                out = await check()
                if out is not None:
                    return out
                await sent.wait()
        return None

    async def cancel(self, iid: int) -> bool:
        """Hides a scheduled message that has not landed anywhere yet. False when it has,
        was cancelled already, or has no due time."""
        def fn(c: sqlite3.Connection) -> list[str] | None:
            r = c.execute("SELECT due_at FROM intercom WHERE id=?", (iid,)).fetchone()
            if r is None or r["due_at"] is None:
                return None
            if c.execute("SELECT 1 FROM intercom_deliveries WHERE intercom_id=? AND "
                         "state != 'pending'", (iid,)).fetchone():
                return None
            # the user's delivery stays pending until read: past its time it has landed
            if r["due_at"] <= now() and not c.execute(
                    "SELECT 1 FROM intercom_deliveries WHERE intercom_id=? AND "
                    "conversation_id IS NOT NULL", (iid,)).fetchone():
                return None
            if not c.execute("UPDATE intercom SET cancelled_at=? WHERE id=? AND "
                             "cancelled_at IS NULL", (now(), iid)).rowcount:
                return None
            cids = [r[0] for r in c.execute(
                "UPDATE intercom_deliveries SET state='cancelled' WHERE intercom_id=? "
                "RETURNING conversation_id", (iid,))]
            _changed(c, self.store, iid, [x for x in cids if x])
            return cids

        return await self.store.write(fn) is not None

    async def cancel_scheduled(self, cid: str) -> list[int]:
        """Every scheduled message of conversation `cid` that has not landed: the ones
        waiting for it (a time limit, its own reminders) and the ones it set for
        others. They stay listed as cancelled. -> the ids cancelled."""
        ids = await self.store.read(lambda c: [r[0] for r in c.execute(
            "SELECT DISTINCT i.id FROM intercom i JOIN intercom_deliveries d ON "
            "d.intercom_id = i.id WHERE d.state='pending' AND i.due_at IS NOT NULL AND "
            "i.cancelled_at IS NULL AND (d.conversation_id=? OR i.from_addr=?)",
            (cid, f"agent:{cid}"))])
        return [iid for iid in ids if await self.cancel(iid)]

    async def cancel_waiting(self, cid: str) -> None:
        """Every message still waiting to land in conversation `cid`, due or not (a
        sub-agent's report, a notice): its delivery there is cancelled, other
        recipients keep theirs. For a conversation that must not wake again."""
        def fn(c: sqlite3.Connection) -> None:
            for r in c.execute("UPDATE intercom_deliveries SET state='cancelled' WHERE "
                               "conversation_id=? AND state='pending' RETURNING intercom_id",
                               (cid,)).fetchall():
                _changed(c, self.store, r[0], [cid])

        await self.store.write(fn)

    async def deliver_idle(self, cid: str) -> None:
        out = await deliver.deliver_idle(self.store, cid)
        if out and out["turn_id"]:
            self.notify()

    async def run(self, tg: anyio.abc.TaskGroup) -> None:
        tg.start_soon(self._timer)

    async def _timer(self) -> None:
        """Delivers what is due (at start: what came due while the app was down), then
        sleeps until the next due time or a new message with one; after a failure it
        logs the reason and waits for the next new message."""
        while True:
            try:
                if await deliver.deliver_due(self.store):
                    self.notify()
                nxt = await deliver.next_due(self.store)
                delay = None if nxt is None else max(0.0, (
                    datetime.fromisoformat(nxt) - datetime.now(timezone.utc)
                ).total_seconds())
            except Exception:
                log.exception("intercom timer failed", extra={"event": "intercom.failed"})
                delay = None
            with anyio.move_on_after(delay):
                await self._wake.wait()
            self._wake = anyio.Event()
