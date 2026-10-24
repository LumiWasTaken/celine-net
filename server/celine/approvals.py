"""Approvals: what the user answers before something runs, from a logged-in session
(GET /api/approvals, POST /api/approvals/{id}/answer; Settings, Vault shows the vault's) or with
`!approve` / `!deny` in the bridged chat. One service, three kinds:

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

The text the user reads is built from fixed sentences and the call's own data, never
from its words. It also names the last tool that read something from outside (a page,
a mail, a chat, a file) in that conversation before it asked, so a request right after
such a read stands out: that is where someone else's words get in.

The tool call waits for the answer (ASK_FOR seconds). The user answers from a logged-in
session (a named token is refused), or in the bridged chat: the bridge takes commands
only from the user's own messages there (bridge/service.py, from_me), never from what
it posts. One request waits at a time. Everything is in memory: a restart forgets it,
and the vault is locked after a restart anyway. Every change is the global event
`approvals.changed`, and a new request a `notice` (events.py) too.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field

from .core.event_types import APPROVALS_CHANGED
from .guard.sources import OUTSIDE   # skills whose results others wrote: one list

log = logging.getLogger("celine.approvals")

ASK_FOR = 120       # seconds a request waits for the user
KINDS = ("vault", "gate", "her")
LABEL = {"vault": "Vault", "gate": "Bastet", "her": "Persona"}
APPROVED_KEPT = 64  # tool calls approved on Bastet's ask, kept so the tool asks no more

SHELL = "put the password of {entry} into a file in the shellbox"
ROTATE = "change the password of {entry}"
SHELL_SHORT = "{entry} password into the shell"
ROTATE_SHORT = "new password for {entry}"
WHY_MAX = 300
COMMAND = re.compile(r"^!(approve|deny)\s*$", re.I)


def _minutes() -> str:
    n = max(1, round(ASK_FOR / 60))
    return f"{n} minute" + ("s" if n != 1 else "")


@dataclass
class Ask:
    id: str
    kind: str               # vault or gate
    action: str             # one of the sentences above, filled in
    facts: list[str]        # the call's own data: an entry's sites, a tool's arguments
    why: str                # her reason, in her words, shown apart (vault); Bastet's (gate)
    short: str              # the action in a few words, for the ping
    conversation: str
    read: str | None        # the last tool that read from outside before it asked
    expires: float          # time.time()
    answer: asyncio.Future = field(repr=False)
    ref: int | None = None  # gate: Bastet's row, so her banner can answer it

    def view(self) -> dict:
        return {"id": self.id, "kind": self.kind, "action": self.action,
                "facts": self.facts, "why": self.why,
                "conversation": self.conversation, "read": self.read,
                "expires_in": max(0, round(self.expires - time.time()))}


class Approvals:
    def __init__(self, cfg, emit=None, ping=None, store=None):
        self.cfg, self.emit, self.ping, self.store = cfg, emit, ping, store
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
        if self.waiting:
            return f"Another request waits for {user}; ask again once it is answered."
        ask = Ask(id=secrets.token_urlsafe(16), kind=kind, action=action, facts=facts,
                  why=why, short=short, conversation=conversation,
                  read=self.read.get(conversation), expires=now + ASK_FOR,
                  answer=asyncio.get_running_loop().create_future(), ref=ref)
        self.waiting[ask.id] = ask
        log.info("approval asked", extra={"event": "approvals.asked", "kind": kind,
                                          "action": action.split(" ")[0]})
        self._changed()
        await self._notice(ask)
        await self._ping(ask)
        try:
            ok = await asyncio.wait_for(asyncio.shield(ask.answer), ASK_FOR)
        except asyncio.TimeoutError:
            ok = None
        finally:
            self.waiting.pop(ask.id, None)
            self._changed()
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

    def decide(self, aid: str, ok: bool, by) -> bool:
        """The user's answer; `by`: the owner (guard/owner.py), anything else a
        TypeError. -> False when no such request waits."""
        from .guard.owner import must
        must(by)
        ask = self.waiting.get(aid)
        if ask is None or ask.answer.done() or time.time() > ask.expires:
            return False
        ask.answer.set_result(bool(ok))
        log.info("approval answered", extra={"event": "approvals.answered", "ok": bool(ok)})
        return True

    def by_ref(self, ref: int) -> str | None:
        """The id of the request that waits for Bastet's row `ref` (her banner)."""
        return next((a.id for a in self.waiting.values()
                     if a.ref == ref and not a.answer.done()), None)

    def command(self, text: str, by) -> str | None:
        """`!approve` or `!deny` from the user's bridged chat (`by`, the Owner the bridge
        made): the one waiting request.
        -> the answer for that chat, or None when the text is no such command (it goes
        to it as a message then)."""
        m = COMMAND.match((text or "").strip())
        if not m:
            return None
        ok = m[1].lower() == "approve"
        live = [a for a in self.waiting.values()
                if not a.answer.done() and time.time() <= a.expires]
        if not live:
            return "No request waits."
        ask = live[0]
        self.decide(ask.id, ok, by)
        return ("Approved: " if ok else "Denied: ") + ask.action + "."

    def view(self, kind: str | None = None) -> list[dict]:
        return [a.view() for a in self.waiting.values() if kind in (None, a.kind)]

    def _changed(self) -> None:
        if self.emit is not None:
            self.emit({"type": APPROVALS_CHANGED})

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

    async def _ping(self, ask: Ask) -> None:
        """The user's phone: the bridged chat, the request's own words only."""
        if self.ping is None:
            return
        try:
            await self.ping(self.message(ask))
        except Exception:
            log.warning("approval ping failed", exc_info=True,
                        extra={"event": "approvals.ping_failed"})

    def message(self, ask: Ask) -> str:
        """Short: what, why, how to answer."""
        what = ask.short or ask.action
        label = LABEL[ask.kind]
        out = f" Read from outside before: {ask.read}." if ask.read else ""
        return (f"{label}: {what}\nWhy: {ask.why}{out}\n"
                f"!approve or !deny ({_minutes()})")
