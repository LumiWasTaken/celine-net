"""Approvals: what the user answers before something runs, from a logged-in session
(GET /api/approvals, POST /api/approvals/{id}/answer; Settings, Vault shows the vault's).
One service, four kinds:

- `vault`, the vault's taps. Only the high-risk actions ask: the password into its
  shellbox (vault__get with shell), where everything in the box and the internet can
  read it, and a new password for an entry (vault__rotate), which changes a live
  account. It says why in each (`why`), and the request shows it apart, as its words.
  Nothing else asks: a new entry, a list of names, a placeholder, typing into its
  browser (only on the entry's own sites, browserd checks that every time), a lock.
- `gate`, Bastet's confirm rule (guard/rules.py): a tool the user listed waits for their
  answer every time. Bastet's words, no reason from her. Its `ref` is her row, so her
  banner in the chat answers it too (guard/banners.py).
- `her`, an edit of herself (me__edit, skills/me.py): her persona, talk and slop list
  are in every chat, so every edit waits for the user, guard or no guard. A call the
  user already approved on Bastet's ask (`call`, its tool call id) is not asked again.
- `share`, a public share (shell__share, skills/shell.py): anyone with its link opens
  it, so it waits for the user, guard or no guard, as an edit of herself does.

Every request but a gate (Bastet's banner is its ask there) shows in the chat that asked,
after the message that made the call, with Approve and Deny: the conversation event
`approval.asked` {approval} (`chat`), and `approval.closed` {id} once it is answered,
stopped or out of time; the snapshot's `approvals` are the ones asked and not closed
(`open_in`). A restart closes what the last run left open (`close_left`).

The text the user reads is built from fixed sentences and the call's own data, never
from its words. It also names the last tool that read something from outside (a page,
a mail, a chat, a file) in that conversation before it asked, so a request right after
such a read stands out: that is where someone else's words get in.

In the user's own turn (turns/waiting.py) the tool call waits for their answer or their
Stop, and the turn's wall clock and the tool's time do not run meanwhile; in any other
turn (a routine, a mission, a sub-agent) it waits ASK_FOR seconds. The user answers from
a logged-in session (a named token is refused). One request waits at a time in a
conversation. A request is in memory: a restart forgets it,
and the vault is locked after a restart anyway. Every change is the global event
`approvals.changed`, and a new request a `notice` (events.py) too.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field

import anyio

from .core.event_types import APPROVAL_ASKED, APPROVAL_CLOSED, APPROVALS_CHANGED
from .guard.sources import OUTSIDE   # skills whose results others wrote: one list

log = logging.getLogger("celine.approvals")

ASK_FOR = 120       # seconds a request waits for the user
KINDS = ("vault", "gate", "her", "share")
LABEL = {"vault": "Vault", "gate": "Bastet", "her": "Persona", "share": "Shares"}
LINE_MAX = 200      # characters of a request's facts on its line in the chat
APPROVED_KEPT = 64  # tool calls approved on Bastet's ask, kept so the tool asks no more

SHELL = "put the password of {entry} into a file in the shellbox"
ROTATE = "change the password of {entry}"
SHELL_SHORT = "{entry} password into the shell"
ROTATE_SHORT = "new password for {entry}"
WHY_MAX = 300


STOPPED = object()   # the turn the user waited on ended before they answered


def _minutes() -> str:
    n = max(1, round(ASK_FOR / 60))
    return f"{n} minute" + ("s" if n != 1 else "")


@dataclass
class Ask:
    id: str
    kind: str               # one of KINDS
    action: str             # one of the sentences above, filled in
    facts: list[str]        # the call's own data: an entry's sites, a tool's arguments
    why: str                # her reason, in her words, shown apart (vault); Bastet's (gate)
    short: str              # the action in a few words, for the notice
    conversation: str
    read: str | None        # the last tool that read from outside before it asked
    expires: float | None   # time.time(); None: it waits for the user (their own turn)
    answer: asyncio.Future = field(repr=False)
    ref: int | None = None  # gate: Bastet's row, so her banner can answer it
    call: str | None = None  # the tool call it is for

    def view(self) -> dict:
        return {"id": self.id, "kind": self.kind, "action": self.action,
                "facts": self.facts, "why": self.why,
                "conversation": self.conversation, "read": self.read,
                "expires_in": None if self.expires is None else
                max(0, round(self.expires - time.time()))}

    def line(self) -> str:
        """Its line in the chat: the action, then the call's own data on one line."""
        what = self.action[:1].upper() + self.action[1:] + "."
        facts = " ".join("; ".join(self.facts).split())
        if len(facts) > LINE_MAX:
            facts = facts[:LINE_MAX - 1].rstrip() + "\u2026"
        return f"{what} {facts}" if facts else what

    def live(self) -> bool:
        return not self.answer.done() and (self.expires is None or
                                           time.time() <= self.expires)


def chat(ask: Ask, message_id: str | None) -> dict:
    """A request as its chat shows it: the kind's name, its line, Approve and Deny,
    after the message `message_id`."""
    return {"id": ask.id, "kind": ask.kind, "label": LABEL[ask.kind], "line": ask.line(),
            "tool_call_id": ask.call, "message_id": message_id}


def _open(rows) -> dict[str, tuple[str, dict]]:
    """(conversation, type, data) rows of approval events, in order -> the requests
    asked and not closed, by id: (conversation, its chat view)."""
    out: dict[str, tuple[str, dict]] = {}
    for cid, kind, data in rows:
        d = json.loads(data)
        if kind == APPROVAL_ASKED:
            out[d["approval"]["id"]] = (cid, d["approval"])
        else:
            out.pop(d["id"], None)
    return out


def open_in(c, cid: str) -> list[dict]:
    """The snapshot's `approvals`: what waits in conversation `cid`, oldest first."""
    return [a for _, a in _open(c.execute(
        "SELECT conversation_id, type, data FROM events WHERE conversation_id=? AND "
        "type IN (?, ?) ORDER BY seq", (cid, APPROVAL_ASKED, APPROVAL_CLOSED))).values()]


async def close_left(store) -> int:
    """At startup: a request the last run left open waits for nobody any more; each
    is closed in its chat. -> how many."""
    def fn(c) -> int:
        left = _open(c.execute(
            "SELECT conversation_id, type, data FROM events WHERE type IN (?, ?) "
            "ORDER BY conversation_id, seq", (APPROVAL_ASKED, APPROVAL_CLOSED)))
        for aid, (cid, _) in left.items():
            store.event(c, cid, APPROVAL_CLOSED, {"id": aid})
        return len(left)
    return await store.write(fn)


class Approvals:
    def __init__(self, cfg, emit=None, store=None):
        self.cfg, self.emit, self.store = cfg, emit, store
        self.waiting: dict[str, Ask] = {}
        self.read: dict[str, str] = {}      # conversation -> last outside tool
        self.approved: dict[str, None] = {}  # calls approved on a gate ask, newest last

    # ---- what the runner tells it -------------------------------------------------------

    def seen(self, conversation: str, tool: str) -> None:
        """A tool's result landed: remember it when it read from outside."""
        if tool.split("__")[0] in OUTSIDE:
            self.read[conversation] = tool

    def forget(self, kind: str = "vault") -> None:
        """The vault's unlock is over: every waiting request of `kind` is denied."""
        for aid, ask in list(self.waiting.items()):
            if ask.kind == kind:
                if not ask.answer.done():
                    ask.answer.set_result(False)
                self.waiting.pop(aid, None)

    # ---- what the tools ask -------------------------------------------------------------

    async def ask(self, action: str, facts: list[str], conversation: str,
                  why: str = "", short: str = "", kind: str = "vault",
                  ref: int | None = None, call: str | None = None) -> str | None:
        """Wait for the user's answer. -> None when approved, else one sentence for its
        tool result: denied, no answer in time (it starts with `{user} did not answer`),
        no reason given (vault), or another one waits. `call`: the tool call it is for;
        one the user approved on Bastet's ask (a gate) is not asked again by its tool."""
        if call and kind != "gate" and call in self.approved:
            del self.approved[call]
            return None
        now = time.time()
        user = self.cfg.user
        why = " ".join((why or "").split())[:WHY_MAX]
        if kind == "vault" and len(why) < 10:
            return (f"Nothing was asked: say why you need it in `why`, one real sentence "
                    f"{user} can judge (what for, which site or script).")
        if any(a.conversation == conversation for a in self.waiting.values()):
            return f"Another request waits for {user}; ask again once it is answered."
        from .turns.waiting import TURN
        turn = TURN.get()
        turn = turn if turn is not None and turn.interactive else None
        ask = Ask(id=secrets.token_urlsafe(16), kind=kind, action=action, facts=facts,
                  why=why, short=short, conversation=conversation,
                  read=self.read.get(conversation),
                  expires=None if turn is not None else now + ASK_FOR,
                  answer=asyncio.get_running_loop().create_future(), ref=ref, call=call)
        self.waiting[ask.id] = ask
        log.info("approval asked", extra={"event": "approvals.asked", "kind": kind,
                                          "action": action.split(" ")[0]})
        self._changed()
        try:
            await self._landed(ask)
            await self._notice(ask)
            ok = await self._answer(ask, turn)
        finally:
            self.waiting.pop(ask.id, None)
            self._changed()
            with anyio.CancelScope(shield=True):
                await self._closed(ask)
        if ok is STOPPED:
            return f"{user} did not answer; nothing was done."
        if ok:
            if call and kind == "gate":
                self.approved[call] = None
                while len(self.approved) > APPROVED_KEPT:
                    del self.approved[next(iter(self.approved))]
            return None
        if ok is None:
            return f"{user} did not answer in {_minutes()}; nothing was done."
        return f"{user} denied it; nothing was done."

    # ---- what the user answers ----------------------------------------------------------

    @staticmethod
    async def _answer(ask: Ask, turn) -> object:
        """True or False: the user's answer; None: no answer in ASK_FOR seconds;
        STOPPED: their turn ended (a shutdown) before they answered. In their own turn
        (`turn`) it waits for them, and the turn's clock and the tool's time are held
        (backends/llm.paused); a Stop cancels the wait (turns/cancel.until_stop)."""
        if turn is None:
            try:
                return await asyncio.wait_for(asyncio.shield(ask.answer), ASK_FOR)
            except asyncio.TimeoutError:
                return None
        from .backends.llm import paused
        with paused(turn.clock):
            stop = asyncio.ensure_future(turn.token.wait())
            try:
                await asyncio.wait({ask.answer, stop},
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop.cancel()
        return ask.answer.result() if ask.answer.done() else STOPPED

    def decide(self, aid: str, ok: bool, by) -> bool:
        """The user's answer; `by`: the owner (guard/owner.py), anything else a
        TypeError. -> False when no such request waits."""
        from .guard.owner import must
        must(by)
        ask = self.waiting.get(aid)
        if ask is None or not ask.live():
            return False
        ask.answer.set_result(bool(ok))
        log.info("approval answered", extra={"event": "approvals.answered", "ok": bool(ok)})
        return True

    def by_ref(self, ref: int) -> str | None:
        """The id of the request that waits for Bastet's row `ref` (her banner)."""
        return next((a.id for a in self.waiting.values()
                     if a.ref == ref and not a.answer.done()), None)

    def view(self, kind: str | None = None) -> list[dict]:
        return [a.view() for a in self.waiting.values() if kind in (None, a.kind)]

    def _changed(self) -> None:
        if self.emit is not None:
            self.emit({"type": APPROVALS_CHANGED})

    async def _landed(self, ask: Ask) -> None:
        """In its chat: `approval.asked`, after the message that made the call (a gate
        is asked on Bastet's banner)."""
        if self.store is None or ask.kind == "gate":
            return
        store = self.store

        def fn(c) -> None:
            r = c.execute("SELECT message_id FROM tool_calls WHERE id=?",
                          (ask.call,)).fetchone() if ask.call else None
            store.event(c, ask.conversation, APPROVAL_ASKED,
                        {"approval": chat(ask, r[0] if r else None)})
        await store.write(fn)

    async def _closed(self, ask: Ask) -> None:
        """Answered, stopped or out of time: `approval.closed` takes it away."""
        if self.store is None or ask.kind == "gate":
            return
        await self.store.append_event(ask.conversation, APPROVAL_CLOSED, {"id": ask.id})

    async def _notice(self, ask: Ask) -> None:
        """The app's notice (events.notice): it opens where the request is answered,
        Settings, Vault for the vault's, her chat (Bastet's banner) for a gate, the chat
        that asked for an edit of herself."""
        if self.store is None:
            return
        from . import homes
        from .events import notice
        await self.store.write(lambda c: notice(
            self.store, c, "approval", LABEL[ask.kind], ask.short or ask.action,
            "/settings/vault" if ask.kind == "vault" else homes.of(c, ask.conversation)["link"],
            f"ask:{ask.id}"))
