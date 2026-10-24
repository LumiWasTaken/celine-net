"""The bridged chat: the user texts Celine from a connector's own chat (WhatsApp:
"Message yourself"; its messages start with the marker) and it answers there. A
connector bridges when its `/admin/info` names a `write_chat`; `part_chars` there is the
longest message it sends.

Prompts are read from the archive (the feed fills it, kb/feed.py), after a cursor: kv
`bridge_cursor`, the archive's last message id the bridge looked at. A message in the
connector's write chat is a prompt when all hold:
  1. the user wrote it (`from_me`), and it does not start with the marker (its own do);
  2. it is newer than the cursor and was written at or after the moment the bridge was
     switched on (kv `bridge_since`, ms). So history is never a prompt: not what the
     feed brings again (the same row), not the phone's older self-chat messages;
  3. it has text.
The cursor and the switch live in the archive's file: the cursor is one of its message
ids. The archive is read after every feed batch and whenever the lease is taken, so a
prompt written while the app starts waits for the lease and is not lost. While another
instance holds the lease, the cursor moves past what it answers.

A prompt becomes an intercom message from the user, with `via` the bridge's source, to
one conversation per bridge ("Bridge (WhatsApp)", trigger key `bridge:<kb>`). A quote-
reply in the chat to a message it sent there (`say`) is a reply in that message's
thread instead. When a turn that took such a message ends (the dispatcher's turn-end
hook), its answer (conversations/tree.py final_words) is sent through the connector's `send`
tool over MCP (the only write path; the tool is not Celine's), every part in order.
What is typed in the app inside that conversation stays in the app.

The switch is per app instance. While it is on, the bridge holds the connector's lease
(renewed every 20 s); an instance without the lease creates no turns and sends nothing,
so two instances never both answer.

It writes to the user here with `say` (`intercom__send` to the user with `urgent`).
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import anyio

from ..conversations import store as conversations
from ..conversations.tree import final_words
from ..guard import owner
from ..intercom import store as rows
from .render import render, split

log = logging.getLogger("celine.bridge")


LEASE_EVERY = 20.0
PART_PAUSE = 1.5
CAPPED = "Stopped at the step limit."


def _now() -> float:
    return time.time()


@dataclass
class Side:
    """One connector's bridge in this instance."""
    kb: Any
    connector: Any
    since: int | None = None        # ms the bridge was switched on, None while off
    cursor: int = 0                 # the archive's last message id looked at
    held: bool = False
    error: str | None = None

    @property
    def title(self) -> str:
        return f"Bridge ({self.kb.title})"

    @property
    def trigger_key(self) -> str:
        return f"bridge:{self.kb.id}"


class Bridge:
    def __init__(self, store, connectors, call: Callable[[str, str, dict], Awaitable[Any]],
                 intercom, resolve=None, title: Callable[[str], str] = str.title,
                 sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
                 command: Callable[[str, Any], Awaitable[str | None]] | None = None):
        self.store = store
        self.connectors = connectors
        self.call = call
        self.intercom = intercom
        # turn id -> the answer text that went out ("" when none): a turn ends again
        # after a Continue of it, and only what it added goes out
        # note: in memory, one entry per bridge turn; a restart between two ends of
        # one turn sends that turn's answer whole
        self._sent: dict[str, str] = {}
        self.resolve = resolve
        self.title = title
        self.sleep = sleep
        # a text command answered by the app itself, not by it: `!approve`, `!deny`
        # (celine/approvals.py), `!freeze`, `!unfreeze` (guard/freeze.py), with the
        # Owner only their own messages make (read). -> the answer, or None: no such
        # command, and the text goes to it as a message
        self.command = command
        self.sides: dict[str, Side] = {}
        for kid, c in connectors.all.items():
            self.sides[kid] = Side(kb=c.kb, connector=c, since=c.kb.file.get_json("bridge_since"),
                                   cursor=c.kb.file.get_json("bridge_cursor", 0))
        self._send, self._recv = anyio.create_memory_object_stream[str](1000)
        self._loop_token = None
        self._reading = anyio.Lock()

    # ---- the switch --------------------------------------------------------------------
    def is_on(self, kid: str) -> bool:
        side = self.sides.get(kid)
        return bool(side) and side.since is not None

    def state(self, kid: str) -> dict[str, Any]:
        side = self.sides.get(kid)
        if side is None:
            return {"on": False, "held": False, "error": None}
        return {"on": self.is_on(kid), "held": side.held, "error": side.error}

    def set_on(self, kid: str, on: bool) -> None:
        """Switch the bridge of one source on or off (blocking: from a thread). On: only
        messages written from now on become prompts."""
        side = self.sides[kid]
        if on and side.since is None:
            side.cursor = self._top(side.kb)
            side.since = int(_now() * 1000)
            side.kb.file.set_json("bridge_cursor", side.cursor)
            side.kb.file.set_json("bridge_since", side.since)
        elif not on:
            side.kb.file.set_json("bridge_since", None)
            side.since, side.held = None, False

    @staticmethod
    def _top(kb) -> int:
        return kb.file.read_sync(lambda c: c.execute(
            "SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0])

    # ---- wiring ------------------------------------------------------------------------
    def attach(self) -> None:
        """Every applied feed batch wakes the bridge (from the feed's thread)."""
        for kid, side in self.sides.items():
            side.connector.on_batch = (lambda _applied, kid=kid: self._from_thread(kid))

    def _from_thread(self, kid: str) -> None:
        if self._loop_token is None:
            return              # not running yet: the first lease reads the archive
        try:
            anyio.from_thread.run_sync(self._send.send_nowait, kid, token=self._loop_token)
        except anyio.WouldBlock:
            pass                # a read is due anyway
        except Exception:
            log.warning("bridge missed a batch", exc_info=True,
                        extra={"event": "bridge.missed"})

    async def run(self) -> None:
        self._loop_token = anyio.lowlevel.current_token()
        self.attach()
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._leases)
            async for kid in self._recv:
                try:
                    await self.read(self.sides[kid])
                except Exception:
                    log.exception("bridge read failed")

    # ---- the lease ---------------------------------------------------------------------
    async def lease(self, side: Side) -> None:
        c = side.connector
        if not self.is_on(side.kb.id):
            if side.held:
                try:
                    await c.request("DELETE", "/admin/bridge/lease")
                except Exception:
                    log.warning("lease release failed", exc_info=True,
                                extra={"event": "bridge.lease_release_failed"})
            side.held, side.error = False, None
            return
        if not (c.info or {}).get("write_chat"):
            side.held, side.error = False, f"The {side.kb.title} connection has no bridged chat."
            return
        try:
            code, data = await c.request("POST", "/admin/bridge/lease")
        except Exception:
            side.held, side.error = False, f"The {side.kb.title} connection is not running."
            return
        if code == 200:
            if not side.held:
                log.info("bridge lease held", extra={"event": "bridge.lease", "kb": side.kb.id})
            side.held, side.error = True, None
            await self.read(side)
        else:
            if side.held or side.error is None:
                log.info("bridge lease refused", extra={"event": "bridge.lease_refused",
                                                        "kb": side.kb.id})
            side.held = False
            side.error = (data or {}).get("error") or "The bridged chat could not start."
            # the instance that holds it answers these
            await self._move(side, await anyio.to_thread.run_sync(self._top, side.kb))

    async def _leases(self) -> None:
        while True:
            for side in self.sides.values():
                await self.lease(side)
            await self.sleep(LEASE_EVERY)

    # ---- prompts -----------------------------------------------------------------------
    def _new(self, side: Side) -> tuple[list[dict[str, Any]], int]:
        """(the prompts after the cursor, oldest first; the archive's last id)."""
        info = side.connector.info or {}
        marker = (info.get("marker") or "").strip()

        def run(c):
            top = c.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
            found = c.execute(
                "SELECT m.id, m.text, m.reply_key FROM messages m JOIN chats ch "
                "ON ch.id = m.chat_id WHERE m.id > ? AND m.id <= ? AND ch.key = ? "
                "AND m.from_me = 1 AND m.ts >= ? AND m.deleted_at IS NULL ORDER BY m.id",
                (side.cursor, top, info.get("write_chat"), side.since)).fetchall()
            return [dict(r) for r in found], top
        found, top = side.kb.file.read_sync(run)
        return [r for r in found if (r["text"] or "").strip()
                and not (marker and r["text"].strip().startswith(marker))], top

    async def read(self, side: Side) -> None:
        """Every prompt after the cursor becomes an intercom message, once."""
        async with self._reading:
            if side.since is None or not side.held:
                return
            found, top = await anyio.to_thread.run_sync(self._new, side)
            for r in found:
                text = r["text"].strip()
                # their own message (from_me, not its own: _new): the only bridged Owner
                answer = await self.command(text, owner.bridged()) \
                    if self.command and text.startswith("!") \
                    else None
                if answer is not None:
                    await self.deliver(side, answer)
                else:
                    await self.prompt(side, text, r["reply_key"])
                await self._move(side, r["id"])
            await self._move(side, top)

    async def _move(self, side: Side, cursor: int) -> None:
        if cursor > side.cursor:
            side.cursor = cursor
            await anyio.to_thread.run_sync(side.kb.file.set_json, "bridge_cursor", cursor)

    async def prompt(self, side: Side, text: str, reply_key: str | None = None) -> str:
        # `via`: the turn that takes it answers in this chat (on_end)
        data = {"via": side.kb.id}
        quoted = await self.store.read(
            lambda c: rows.sent_as(c, side.kb.id, reply_key)) if reply_key else None
        if quoted is not None:
            out = await self.intercom.send("user", "", text, reply_to=quoted, data=data)
        else:
            cid = await conversations.for_trigger(self.store, side.trigger_key,
                                                  side.title)
            out = await self.intercom.send("user", f"agent:{cid}", text, data=data)
        log.info("bridge prompt", extra={"event": "bridge.prompt", "kb": side.kb.id})
        return out["turn_id"]

    # ---- answers -----------------------------------------------------------------------
    async def on_end(self, cid: str, turn_id: str | None) -> None:
        """The dispatcher's turn-end hook: the answer of a turn that took a prompt."""
        if turn_id is None:
            return
        r = await self.store.read(lambda c: c.execute(
            "SELECT state, reason FROM turns WHERE id = ?", (turn_id,)).fetchone())
        if r is not None:
            await self.turn_ended(turn_id, r["state"], r["reason"])

    async def turn_ended(self, turn_id: str, state: str, reason: str | None) -> bool:
        """Deliver the answer of a turn the bridge started, or of a Continue of one: the
        part not sent yet. True when something new went out or the turn was new."""
        via, before, continued = await self._origin(turn_id)
        side = self.sides.get(via or "")
        if side is None:
            return False
        first = turn_id not in self._sent
        before = self._sent.get(turn_id, before)
        if first:
            self._sent[turn_id] = before
        if state in ("done", "capped"):
            text = await self.store.read(lambda c: final_words(c, turn_id))
            if state == "capped" and not text:
                if first:
                    await self.deliver(side, CAPPED)
                return first
            # a Continue of its reply carries its text on; a Regenerate stays in the app
            if continued and not text.startswith(before):
                return False
            rest = text[len(before):] if before and text.startswith(before) else text
            if text != before and rest.strip():
                self._sent[turn_id] = text
                await self.deliver(side, render(rest.strip(), self.resolve, self.title))
                return True
        elif state == "failed" and first:
            why = (reason or "something went wrong").rstrip(".")
            await self.deliver(side, f"The turn failed: {why}.")
        return first

    async def _origin(self, turn_id: str) -> tuple[str | None, str, bool]:
        """(the bridged chat the turn answers, the text already sent there, whether it
        carries on another turn's reply). A turn that took a prompt answers its chat; a
        Continue of such a reply (a new turn, `again` the reply) answers the same chat,
        the reply's answer already sent."""
        via = await self.store.read(lambda c: rows.via(c, turn_id))
        if via is not None:
            return via, "", False
        r = await self.store.read(lambda c: c.execute(
            "SELECT m.turn_id FROM turns t JOIN messages m ON m.id = t.again "
            "WHERE t.id = ?", (turn_id,)).fetchone())
        if r is None or r[0] is None:
            return None, "", False
        up, _, _ = await self._origin(r[0])         # a Continue of a Continue
        if up is None:
            return None, "", False
        sent = self._sent.get(r[0])
        if sent is None:
            sent = await self.store.read(lambda c: final_words(c, r[0]))
        return up, sent, True

    async def send_image(self, conversation_id: str, data: bytes, filename: str,
                         media_type: str) -> bool:
        """An image Celine made in a bridge's conversation goes to that chat at once.
        True when it was sent."""
        key = await self.store.read(lambda c: c.execute(
            "SELECT trigger_key FROM conversations WHERE id = ?",
            (conversation_id,)).fetchone())
        side = next((s for s in self.sides.values()
                     if key and s.trigger_key == key[0]), None)
        if side is None or not side.held or not self.is_on(side.kb.id):
            return False
        try:
            await self.call(side.connector.server, "send_image", {
                "data": base64.b64encode(data).decode(), "filename": filename,
                "media_type": media_type})
        except Exception as e:
            side.error = str(e)
            log.warning("bridge image failed", exc_info=True,
                        extra={"event": "bridge.image_failed", "kb": side.kb.id})
            return False
        log.info("bridge image sent", extra={"event": "bridge.image", "kb": side.kb.id})
        return True

    # ---- its messages to the user -----------------------------------------------------
    def available(self) -> list[str]:
        """The bridges it can write to now: switched on and held."""
        return [kid for kid, side in self.sides.items() if side.held and self.is_on(kid)]

    async def say(self, text: str) -> tuple[str, list[str]]:
        """The agent's message to the user, into the bridged chat that is on. -> (the source,
        the chat's keys of the parts sent). ValueError with one sentence when no chat
        is reached."""
        text = (text or "").strip()
        if not text:
            raise ValueError("There is nothing to send.")
        open_ = self.available()
        if not open_:
            raise ValueError("No bridged chat is on.")
        side = self.sides[open_[0]]
        sent = await self.deliver(side, render(text, self.resolve, self.title))
        if not sent:
            raise ValueError(side.error or "The app could not send the message.")
        log.info("bridge said", extra={"event": "bridge.said", "kb": side.kb.id})
        return side.kb.id, sent

    async def deliver(self, side: Side, text: str) -> list[str]:
        """Send every part, in order, through the connector's send tool. -> the chat's
        keys of the parts sent (a part without a key counts as "")."""
        if not side.held:
            return []
        sent: list[str] = []
        for i, part in enumerate(split(text, int(side.connector.info["part_chars"]))):
            if i:
                await self.sleep(PART_PAUSE)
            try:
                res = await self.call(side.connector.server, "send", {"text": part})
            except Exception as e:
                side.error = str(e)
                log.warning("bridge send failed", exc_info=True,
                            extra={"event": "bridge.send_failed", "kb": side.kb.id})
                break
            keys = (getattr(res, "structured_content", None) or {}).get("sent") or [""]
            sent += [str(k) for k in keys]
        return sent
