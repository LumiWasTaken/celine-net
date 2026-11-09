"""Approvals: what the user answers before something runs, from a logged-in session
(POST /api/approvals/{id}/answer). One service, these kinds:

- `vault`, the vault's taps. Only the high-risk actions ask: the password into its
  shellbox (vault__get with shell), where everything in the box and the internet can
  read it, and a new password for an entry (vault__rotate), which changes a live
  account. Each needs the agent's reason (`why`), shown as its Reason fact. Nothing else asks:
  a new entry, a list of names, a placeholder, typing into its browser (only on the
  entry's own sites, browserd checks that every time), a lock.
- `gate`, Bastet's confirm rule (guard/rules.py): a tool the user listed waits for their
  answer every time. Its `ref` is Bastet's row, so her banner in the chat answers it too
  (guard/banners.py).
- `agent`, an edit of the agent itself (me__edit, skills/me.py): its persona, talk and slop list
  are in every chat, so every edit waits for the user, guard or no guard. A call the
  user already approved on Bastet's ask (`call`, its tool call id) is not asked again.
- `share`, a public share: anyone with its link opens it, so it waits for the user,
  guard or no guard, as an edit of the agent itself does.
- `settings`, a change of the settings (settings__propose, skills/settings.py): every
  one waits for the user, guard or no guard; each field is a fact, before and after
  (a secret's value never: its state), and the agent's reason.
- `model`, work on a cloud model the agent picked itself (settings/agent_models.py ask_cloud):
  a sub-agent's start or resume, models__ask, code__start, missions__start, a plan step's
  model. Every use waits for the user; a model in a role's list (unless general
  `ask_listed_cloud` is on), the conversation's own or one the user set on a step of its
  plan never asks.

Every request is one card (`Ask.card`): a title "{who} asks to {action}.", facts as
(label, value) pairs from the call's own data, its whole long text apart (`detail`,
opened under Details, never cut), why the kind is asked (HELP, the server fills the agent's
name), where its work lives (`where`, the root's name) and the link that opens
it. Every request but a gate (Bastet's banner is its ask there) shows in the chat that
asked, after the message that made the call: the conversation event `approval.asked`
{approval} (the card), and `approval.closed` {id} once it is answered, stopped or out
of time; the snapshot's `approvals` are the ones asked and not closed (`open_in`). A
restart closes what the last run left open (`close_left`). Waiting for you
(celine/waits.py) lists every card that waits.

The text the user reads is built from fixed sentences and the call's own data, never
from its words. It also names the last tool that read something from outside (a page,
a mail, a chat, a file) in that conversation before it asked, so a request right after
such a read stands out: that is where someone else's words get in.

Inside any turn (turns/waiting.py) the tool call waits for the user's answer or a Stop,
and the turn's wall clock and the tool's time do not run meanwhile: a chat or a mission
waits until they answer (a mission's time limit counts the wait off,
missions/watch.py); a routine's run ends at its own time limit (schedules/runs.py), its
reason ASK_LAPSED. Outside a turn it waits ASK_FOR seconds. The user answers from a
logged-in session (a named token is refused). One request waits at a time in a
conversation. A request is in memory: a restart forgets it, and the vault is locked
after a restart anyway. Every change is the global event `approvals.changed`; a new
request is a `notice` (events.py), and its close one more with the same tag, closed,
that says how it ended (push.py sends that one even to a focused page, silent).
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

ASK_FOR = 120       # seconds a request outside a turn waits for the user
KINDS = ("vault", "gate", "agent", "share", "settings", "model")
APPROVED_KEPT = 64  # tool calls approved on Bastet's ask, kept so the tool asks no more

# why each kind is asked and where that changes; {{name}}: the agent's name
HELP = {
    "vault": ("A password in a file of the shellbox can be read by everything there, "
              "and a new password changes a live account. These two vault steps always "
              "wait for you."),
    "gate": ("You put this tool on Ask you first in Settings, Guard. Take it off that "
             "list to let it run without asking."),
    "agent": ("{{name}}'s persona, example lines and banned words go into every chat, so "
              "every edit waits for you. You edit them yourself in Settings, {{name}}."),
    "share": ("Anyone with the link opens a public share, so each one waits for you. A "
              "private share opens only for you and is not asked about."),
    "settings": ("A change {{name}} proposes is saved only once you approve it. You can make "
                 "the same change yourself in Settings."),
    "model": ("This model runs on a server outside your network. A model from your role "
              "lists asks only while Ask before cloud models from your lists is on; turn "
              "Cloud off for a provider you run yourself."),
}

SHELL = "put the password of {entry} into a file in the shellbox"
ROTATE = "change the password of {entry}"
READ_BEFORE = "Read right before"   # the fact that names the last outside read
REASON = "Reason"
# a run's reason when its time limit ran out while a request waited in it
ASK_LAPSED = "The time limit ran out before your answer. {title}"
# the closing notice's body: approved, denied, no answer (a Stop, a time limit, a restart)
CLOSED = {True: "Approved.", False: "Denied.", None: "No answer."}

STOPPED = object()   # the turn that waited ended before they answered


def _minutes() -> str:
    n = max(1, round(ASK_FOR / 60))
    return f"{n} minute" + ("s" if n != 1 else "")


@dataclass
class Ask:
    id: str
    kind: str               # one of KINDS
    title: str              # "{who} asks to {action}."
    facts: list[tuple[str, str]]   # (label, value): the call's own data, whole
    detail: tuple[str, str] | None  # (title, text): its long text, whole
    help: str               # HELP[kind], filled
    conversation: str
    expires: float | None   # time.time(); None: it waits inside a turn
    answer: asyncio.Future = field(repr=False)
    ref: int | None = None  # gate: Bastet's row, so her banner can answer it
    call: str | None = None  # the tool call it is for
    home: str | None = None  # where its conversation lives (homes.of), and the link
    link: str | None = None
    root: str | None = None  # the top of its agent_of chain (homes.root): a mission, a run
    where: str | None = None  # the root by its name: "Mission Night"

    def card(self, message_id: str | None = None) -> dict:
        """What every place draws of it: the chat after the message `message_id`, the
        Vault page, Waiting for you."""
        return {"id": self.id, "kind": self.kind, "title": self.title,
                "facts": [{"label": k, "value": v} for k, v in self.facts],
                "detail": {"title": self.detail[0], "text": self.detail[1]}
                if self.detail else None,
                "help": self.help, "where": self.where, "home": self.home,
                "link": self.link, "tool_call_id": self.call, "message_id": message_id}

    def live(self) -> bool:
        return not self.answer.done() and (self.expires is None or
                                           time.time() <= self.expires)


def _open(rows) -> dict[str, tuple[str, dict]]:
    """(conversation, type, data) rows of approval events, in order -> the requests
    asked and not closed, by id: (conversation, its card)."""
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
    is closed in its chat, and its notice says No answer. -> how many."""
    from .events import notice

    def fn(c) -> int:
        left = _open(c.execute(
            "SELECT conversation_id, type, data FROM events WHERE type IN (?, ?) "
            "ORDER BY conversation_id, seq", (APPROVAL_ASKED, APPROVAL_CLOSED)))
        for aid, (cid, card) in left.items():
            store.event(c, cid, APPROVAL_CLOSED, {"id": aid})
            notice(store, c, "approval", card.get("title"), CLOSED[None], card.get("link"),
                   f"ask:{aid}", closed=True)
        return len(left)
    return await store.write(fn)


def asked(services) -> frozenset[str]:
    """Approvals.conversations of the app's service, empty without one: the lists
    (conversations, missions, runs, background tasks) mark those rows."""
    a = getattr(services, "approvals", None)
    return a.conversations() if a is not None else frozenset()


def _where(c, cid: str) -> dict:
    """{home, link, root, where} of conversation `cid` (homes.py); all None when it is
    gone. `where`: its root by its name."""
    from . import homes
    top = homes.root(c, cid)
    if top is None:
        return {"home": None, "link": None, "root": None, "where": None}
    name = None
    if top["kind"] == "mission":
        r = c.execute("SELECT name FROM missions WHERE conversation_id=?",
                      (top["id"],)).fetchone()
        name = f"Mission {r[0]}" if r else None
    elif (top["trigger_key"] or "").startswith("run:"):
        r = c.execute("SELECT label FROM runs WHERE id=?",
                      (top["trigger_key"][4:],)).fetchone()
        name = f"Routine {r[0]}" if r else None
    if name is None:
        title = c.execute("SELECT title FROM conversations WHERE id=?",
                          (top["id"],)).fetchone()[0] or "Untitled"
        name = f"{'Chat' if top['kind'] == 'chat' else 'Background task'} {title}"
    return {**homes.of(c, cid), "root": top["id"], "where": name}


def _read_label(tool: str) -> str:
    from .settings.tools_meta import TOOLS, humanize
    return TOOLS.get(tool, {}).get("label") or humanize(tool)


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

    def _help(self, kind: str) -> str:
        sub = getattr(self.cfg, "substitute", None)
        return sub(HELP[kind]) if sub is not None else HELP[kind]

    async def ask(self, kind: str, conversation: str, action: str, *,
                  facts: list[tuple[str, str]] | tuple = (),
                  detail: tuple[str, str] | None = None, why: str = "",
                  ref: int | None = None, call: str | None = None) -> str | None:
        """Wait for the user's answer to "{who} asks to {action}.". `facts`: (label,
        value) pairs of the call's own data, whole; `detail`: (title, text), its long
        text; `why`: the agent's reason, its Reason fact (the vault needs one). -> None when
        approved, else one sentence for its tool result: denied, no answer (it starts
        with `{user} did not answer`), no reason given (vault), or another one waits.
        `call`: the tool call it is for; one the user approved on Bastet's ask (a gate)
        is not asked again by its tool, but a change of settings is: its request shows
        each field before and after."""
        if call and kind not in ("gate", "settings") and call in self.approved:
            del self.approved[call]
            return None
        user = self.cfg.user
        why = (why or "").strip()
        if kind == "vault" and len(why) < 10:
            return (f"Nothing was asked: say why you need it in `why`, one real sentence "
                    f"{user} can judge (what for, which site or script).")
        where = await self.store.read(lambda c: _where(c, conversation)) \
            if self.store is not None else {}
        # no await from this check to the insert: two calls at once cannot both pass
        if any(a.conversation == conversation for a in self.waiting.values()):
            return f"Another request waits for {user}; ask again once it is answered."
        from .turns.waiting import TURN
        turn = TURN.get()
        who = getattr(self.cfg, "name", "") or ""
        rows = [*facts, *([(REASON, why)] if why else [])]
        if (read := self.read.get(conversation)) is not None:
            rows.append((READ_BEFORE, _read_label(read)))
        ask = Ask(id=secrets.token_urlsafe(16), kind=kind,
                  title=f"{who} asks to {action}." if who else
                  action[:1].upper() + action[1:] + ".",
                  facts=rows, detail=detail, help=self._help(kind),
                  conversation=conversation,
                  expires=None if turn is not None else time.time() + ASK_FOR,
                  answer=asyncio.get_running_loop().create_future(), ref=ref, call=call,
                  home=where.get("home"), link=where.get("link"), root=where.get("root"),
                  where=where.get("where"))
        self.waiting[ask.id] = ask
        log.info("approval asked", extra={"event": "approvals.asked", "kind": kind,
                                          "action": action.split(" ")[0]})
        self._changed()
        ok: object = STOPPED
        try:
            await self._landed(ask)
            await self._notice(ask)
            ok = await self._answer(ask, turn)
        finally:
            self.waiting.pop(ask.id, None)
            self._changed()
            with anyio.CancelScope(shield=True):
                await self._closed(ask, ok if isinstance(ok, bool) else None)
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
        """True or False: the user's answer; None: no answer in ASK_FOR seconds (outside
        a turn); STOPPED: its turn ended (a Stop, a time limit, a shutdown) before they
        answered. Inside a turn (`turn`) it waits for them, and the turn's clock and the
        tool's time are held (backends/llm.paused); a Stop cancels the wait
        (turns/cancel.until_stop)."""
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
        """The cards that wait, of `kind` or all, oldest first."""
        return [a.card() for a in self.waiting.values() if kind in (None, a.kind)]

    def conversations(self) -> frozenset[str]:
        """The conversations a request waits in, and the roots of those (a mission, a
        run, a background task): their rows say they wait for the user."""
        return frozenset(x for a in self.waiting.values()
                         for x in (a.conversation, a.root) if x)

    def in_turns(self) -> int:
        """How many wait inside a turn: those turns hold no turn slot
        (turns/dispatcher.py)."""
        return sum(1 for a in self.waiting.values() if a.expires is None)

    def roots(self) -> frozenset[str]:
        """The roots a request waits under inside a turn: a mission's time limit counts
        that wait off (missions/watch.py)."""
        return frozenset(a.root for a in self.waiting.values()
                         if a.root and a.expires is None)

    def lapsed(self, root: str) -> str | None:
        """ASK_LAPSED with the title of the request that waits under `root` (a run's
        conversation), None when none does: a run's reason when its time limit ends it
        meanwhile (schedules/runs.py)."""
        a = next((a for a in self.waiting.values() if a.root == root), None)
        return ASK_LAPSED.format(title=a.title) if a is not None else None

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
                        {"approval": ask.card(r[0] if r else None)})
        await store.write(fn)

    def _link(self, c, ask: Ask) -> str | None:
        """Where its notice opens: Settings, Vault for the vault's, else its chat (for a
        gate, Bastet's banner there)."""
        from . import homes
        return "/settings/vault" if ask.kind == "vault" else \
            homes.of(c, ask.conversation)["link"]

    async def _closed(self, ask: Ask, ok: bool | None) -> None:
        """Answered, stopped or out of time: `approval.closed` takes it away from its
        chat, and the closing notice replaces its notice (`ok`: the answer, None
        without one)."""
        if self.store is None:
            return
        from .events import notice
        store = self.store

        def fn(c) -> None:
            if ask.kind != "gate":
                store.event(c, ask.conversation, APPROVAL_CLOSED, {"id": ask.id})
            notice(store, c, "approval", ask.title, CLOSED[ok], self._link(c, ask),
                   f"ask:{ask.id}", closed=True)
        await store.write(fn)

    async def _notice(self, ask: Ask) -> None:
        """The app's notice (events.notice): its title the card's, its body where its
        work lives; it opens where the request is answered."""
        if self.store is None:
            return
        from .events import notice
        await self.store.write(lambda c: notice(
            self.store, c, "approval", ask.title, ask.where, self._link(c, ask),
            f"ask:{ask.id}"))
