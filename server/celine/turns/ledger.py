"""The per-turn research ledger for the Messages archive and notebook tools.

One Ledger per running turn and tool family (the Messages archive, the notebook): every
call's normalised spec and the refs each call showed (messages; for the notebook, notes
and their text, `note_refs`). It lets a tool answer "same as call 2. Nothing new." instead of
repeating itself (a repeat is not a call), drop match windows it has already seen, and end every result with a
progress line (searches so far, messages seen, and a nudge to change the angle when two
searches in a row brought nothing new). There is no limit on searches that find
something: the user wants deep work, and the context builder (turns/context.py) keeps
the context in size. Searches that keep finding nothing new, of the whole archive or inside one
chat, get a note and then are not run (`dry_streak`). The notebook's searches follow
the same rule (skills/notebook.py).

Durable by construction: the runner seeds a ledger from the turn's stored tool calls,
so a turn resumed after a restart continues with the same counts.

Compaction aware: after every context build the runner tells the ledger which message
refs are in its view right now (`set_view`): refs of tool results sent verbatim. A
result shortened to a stub, or summarised away, no longer counts as shown, so `search`
shows it again, a repeated call is no longer "same as call N", and `scroll` gets it
again.
"""
from __future__ import annotations

import json
import re
import zlib
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

MSG_REF = re.compile(r"\[([a-z0-9_-]+):(\d+)\]")
SEARCH_TOOLS = {"search"}           # the tools that count as a search
ARCHIVE = "chat_archive__"
NOTEBOOK = "notebook__"


@dataclass
class Call:
    n: int
    tool: str
    spec: str
    refs: set[str] = field(default_factory=set)
    new: int = 0
    search: bool = False
    broad: bool = False          # a search over the whole archive or whole sources, nobody named
    dry: bool = False            # a search that brought nothing new or nothing that fits
    refused: bool = False        # a search that was not run (blocked)


NUDGE_AFTER = 3        # dry searches in a row: the result says to narrow down or explore
BLOCK_AFTER = 6        # ... the next search of the whole archive is not run
STOP_AFTER = 8         # ... no search is run: it answers (turns/runner.py)
BLOCKED = "Not run: no search of the whole archive now"
STOPPED = "Not run: no search now"
ENOUGH_SEEN = 20       # messages seen before two calls with nothing new say to answer


class Ledger:
    def __init__(self, turn_id: str, prefix: str = ARCHIVE):
        self.turn_id = turn_id
        self.prefix = prefix                     # the tool family's wire name prefix
        self.searches = 0
        self.calls: list[Call] = []
        self.shown: dict[str, int] = {}          # ref -> call that first showed it
        # refs in tool results it can see right now (verbatim, not stubs), set by the
        # runner after each context build; None outside a turn (then `shown` decides)
        self.view: set[str] | None = None
        # where this turn's searches landed: chat ref -> {"name", "hits"}; best hit refs
        self.leads: dict[str, dict[str, Any]] = {}
        self.best: list[str] = []
        # the person this turn is about: every account of the one person a look-up
        # found, or of written_by; reads mark their lines
        self.focus: set[str] = set()
        # person and chat references a look-up showed: one passed as a message
        # reference is answered with what it is
        self.named: set[str] = set()

    # ---- bookkeeping -----------------------------------------------------------------
    @staticmethod
    def spec(tool: str, args: dict[str, Any]) -> str:
        """Order-, case- and whitespace-insensitive, so a reworded repeat is caught."""
        def norm(v: Any) -> Any:
            if isinstance(v, str):
                return " ".join(v.casefold().split())
            if isinstance(v, (list, tuple)):
                return sorted({json.dumps(norm(x), sort_keys=True) for x in v if x not in
                               (None, "", [])})
            return v
        clean = {k: norm(v) for k, v in sorted(args.items()) if v not in (None, "", [])}
        return json.dumps({"tool": tool, **clean}, sort_keys=True, ensure_ascii=False)

    def same_as(self, spec: str) -> Call | None:
        """An earlier identical call whose result it can still see."""
        return next((c for c in self.calls
                     if c.spec == spec and not c.refused and self.visible(c)), None)

    def visible(self, call: Call) -> bool:
        return self.view is None or all(r in self.view for r in call.refs)

    def in_view(self, ref: str) -> bool:
        """Whether the message is in its context now: shown this turn and not shortened
        away, or (after a context build) in any tool result sent verbatim."""
        return ref in self.view if self.view is not None else ref in self.shown

    def unseen(self, refs: list[str]) -> list[str]:
        """Refs not shown in this turn, or shown and since shortened out of its view."""
        return [r for r in refs if r not in self.shown
                or (self.view is not None and r not in self.view)]

    def set_view(self, messages: list[dict]) -> None:
        """The refs of every tool result in the request as built (stubs excluded)."""
        from .compact import STUB_HEAD
        from .verify import tool_text
        view: set[str] = set()
        for m in messages:
            c = tool_text(m)
            if c and not c.startswith(STUB_HEAD):
                view.update(f"{a}:{b}" for a, b in MSG_REF.findall(c))
        self.view = view

    def seen_in(self, refs: list[str]) -> list[int]:
        return sorted({self.shown[r] for r in refs if r in self.shown})

    def begin(self, tool: str, spec: str, *, search: bool) -> Call:
        """Claim a call number before running, so an identical call running at the
        same time (two in one step) is answered "same as call N"."""
        c = Call(n=len(self.calls) + 1, tool=tool, spec=spec, search=search)
        self.calls.append(c)
        if search:
            self.searches += 1
        return c

    def refuse(self, spec: str, *, broad: bool) -> None:
        """A search that was not run: it counts on in the run of dry searches (asking
        again does not reset it) and costs no search."""
        self.calls.append(Call(n=len(self.calls) + 1, tool="search", spec=spec,
                               broad=broad, dry=True, refused=True))

    def refund(self, call: Call) -> None:
        """An ambiguous name or a bad argument does not cost a search."""
        if call.search:
            call.search = False
            self.searches -= 1

    def finish(self, call: Call, content: str, refs: set[str] | None = None) -> Call:
        """`refs`: what the call showed; by default the message refs in its result."""
        call.refs = refs if refs is not None else {
            f"{m[1]}:{m[2]}" for m in MSG_REF.finditer(content)}
        call.new = len([r for r in call.refs if r not in self.shown])
        if call.search:
            call.dry = call.new == 0
        for r in call.refs:
            self.shown.setdefault(r, call.n)
        if self.view is not None:
            self.view |= call.refs
        return call

    def record(self, tool: str, spec: str, content: str, *, search: bool,
               refs: set[str] | None = None) -> Call:
        return self.finish(self.begin(tool, spec, search=search), content, refs)

    # ---- narrowing: searches that keep finding nothing ------------------------------
    def note_search(self, call: Call, *, broad: bool, weak: bool,
                    episodes: list[dict[str, Any]]) -> None:
        """After a search: whether it was broad and dry, and the chats and hits it showed
        (the leads a narrower next step can use)."""
        call.broad = broad
        call.dry = weak or call.new == 0
        for i, e in enumerate(episodes):
            lead = self.leads.setdefault(e["chat"], {"name": e.get("name") or "?",
                                                     "kind": e.get("kind") or "chat",
                                                     "hits": 0})
            lead["hits"] += max(1, len(e.get("hits") or []))
            if i == 0 and not weak and e.get("hits"):
                self.best = [h for h in self.best if h != e["hits"][0]] + [e["hits"][0]]

    def dry_streak(self) -> tuple[int, bool]:
        """Searches in a row, most recent last, that found nothing new, of the whole archive or
        inside one chat, and whether all of them searched the whole archive. A call that showed
        new messages (a search, a read, a scroll) ends the run. Calls that showed nothing
        new (a count, a look-up, a read of what it has) do not, so a sweep of dry
        searches is not reset by them."""
        n, broad = 0, True
        for c in reversed(self.calls):
            if c.tool == "search" and (c.search or c.refused):
                if not c.dry:
                    break
                n += 1
                broad = broad and c.broad
            elif c.new:
                break
        return n, broad

    def leads_text(self) -> str:
        """Concrete narrower next steps from what this turn already found."""
        parts = []
        top = sorted(self.leads.items(), key=lambda kv: -kv[1]["hits"])[:3]
        if top:
            parts.append("chats where your searches landed: " + ", ".join(
                f'{ref} "{v["name"]}" ({v["kind"]})' for ref, v in top)
                + '. Search one of them with scope=["<chat>"]')
        if self.best:
            parts.append(f'scroll around your best match: '
                         f'chat_archive__scroll(ref="{self.best[-1]}")')
        parts.append("chat_archive__look_up for a person or chat the question names, then "
                     "written_by or scope with its references")
        parts.append('chat_archive__count(by="chat", ...) with the words, to see where they '
                     'occur')
        return ". ".join(parts)

    def narrow_note(self) -> str | None:
        """The note after a dry search, from the third in a row on."""
        n, broad = self.dry_streak()
        if n < NUDGE_AFTER:
            return None
        if broad:
            return (f"{n} searches of the whole archive in a row found nothing new. Searching "
                    f"everything again with other words will not help. Narrow down with "
                    f"what you have: {self.leads_text()}. Or answer with what you found and "
                    f"say what is missing. Write one short line about what you try next.")
        return (f"{n} searches in a row found nothing new. Other words, dates or chats "
                f"will not change that. Read what you found: {self.leads_text()}. Or "
                f"answer with what you found and say what is missing. After "
                f"{STOP_AFTER} in a row, no search is run.")

    def blocked(self, broad: bool) -> str | None:
        """Why the next search is not run, or None: after BLOCK_AFTER dry searches in a
        row a search of the whole archive, after STOP_AFTER any search."""
        n, _ = self.dry_streak()
        if n >= STOP_AFTER:
            return (f"{STOPPED}: {n} searches in a row found nothing new, and another one "
                    f"would not either. Answer with what you found and say what is "
                    f"missing.")
        if broad and n >= BLOCK_AFTER:
            return (f"{BLOCKED}: {n} searches in a row found nothing new, and another one "
                    f"would not either. Narrow down instead: {self.leads_text()}. Or answer "
                    f"with what you found and say what is missing.")
        return None

    def footer(self) -> str:
        others = sum(1 for c in self.calls if not c.search and not c.refused)
        line = (f"progress: {self.searches} search{'es' if self.searches != 1 else ''} and "
                f"{others} other Messages archive calls this turn, "
                f"{len(self.shown):,} messages seen")
        done = [c for c in self.calls if not c.refused]
        searches = [c for c in self.calls if c.search]
        if len(done) >= 2 and done[-1].new == 0 and done[-2].new == 0 \
                and len(self.shown) >= ENOUGH_SEEN:
            line += (". The last two calls showed nothing new. You have enough to answer; "
                     "answer now, or change the angle if one question is still open")
        elif len(searches) >= 2 and searches[-1].new == 0 and searches[-2].new == 0 \
                and self.dry_streak()[0] < NUDGE_AFTER:          # else narrow_note says it
            line += (". Two searches in a row found nothing new: change the angle (other "
                     "about sentences, words or people from what you read, another period, "
                     "the counter-evidence), or answer if every angle is covered")
        return line

    # ---- durability ------------------------------------------------------------------
    def seed(self, records: list[tuple[str, str, str | None, str]]) -> None:
        """(wire name, arguments json, result content, state) of calls already made this
        turn, of this ledger's tool family."""
        for name, arguments, content, state in records:
            if not name.startswith(self.prefix) or content is None:
                continue
            tool = name[len(self.prefix):]
            try:
                args = json.loads(arguments or "{}")
            except ValueError:
                args = {}
            args = args if isinstance(args, dict) else {}
            spec = self.spec(tool, args)
            if tool in SEARCH_TOOLS and content.startswith((BLOCKED, STOPPED)):
                self.refuse(spec, broad=self.prefix == ARCHIVE and _broad(args))
                continue
            if content.startswith("Not run:"):
                continue              # refused before the tool (turns/tools.py)
            if self.prefix == NOTEBOOK:
                if tool == "search" or (tool == "read" and state == "ok"):
                    self.record(tool, spec, content, search=tool == "search",
                                refs=note_refs(tool, args, content))
                continue
            if content.startswith("same as"):
                continue              # a repeat answered without running: not a call
            self.record(tool, spec, content, search=tool in SEARCH_TOOLS)


NO_NOTE = "No note contains"


def note_refs(tool: str, args: dict, content: str) -> set[str]:
    """What a notebook call showed (skills/notebook.py writes the results this reads):
    a search the notes it hit, one `path - summary` line and one snippet line each, then
    a blank line before any note; a read the note and its text, so a read shows new text
    also of a note a search already hit, and again after the note changed."""
    if tool == "search":
        if content.startswith(NO_NOTE):
            return set()
        hits = content.split("\n\n")[0].split("\n")[::2]
        return {f"note:{line.partition(' - ')[0]}" for line in hits}
    path = str(Path(str(args.get("path") or "")))
    return {f"note:{path}", f"note:{path}#{zlib.crc32(content.encode()):08x}"}


def _broad(args: dict) -> bool:
    """A search of the whole archive or whole sources, nobody named (skills/archive.py): its
    scope holds no chat reference and it has no written_by."""
    scope = args.get("scope")
    scope = [scope] if isinstance(scope, str) else scope or []
    return not args.get("written_by") and all(":" not in str(s) for s in scope)


_open: dict[tuple[str, str], Ledger] = {}


def open_for(turn_id: str, records: list[tuple[str, str, str | None, str]] = ()) -> None:
    """The turn's ledgers, one per tool family, seeded from its stored calls (a resumed
    or continued turn keeps its counts and its repeat detection)."""
    for prefix in (ARCHIVE, NOTEBOOK):
        led = Ledger(turn_id, prefix)
        led.seed(list(records))
        _open[(turn_id, prefix)] = led


def get(turn_id: str | None, prefix: str = ARCHIVE) -> Ledger:
    """The running turn's ledger of one tool family; outside a turn (tests, the API) a
    throwaway one."""
    led = _open.get((turn_id or "", prefix))
    return led if led is not None else Ledger(turn_id or "", prefix)


def view(turn_id: str, messages: list[dict]) -> None:
    """After a context build: what of the archive it can see now (compaction aware)."""
    led = _open.get((turn_id, ARCHIVE))
    if led is not None:
        led.set_view(messages)


def close(turn_id: str) -> None:
    for prefix in (ARCHIVE, NOTEBOOK):
        _open.pop((turn_id, prefix), None)


async def records_for(store, turn_id: str, last: int = -1
                      ) -> list[tuple[str, str, str | None, str]]:
    """This turn's tool calls, oldest first, from the database: (wire name, arguments
    json, result content, state). `last`: only the newest that many."""
    def fn(c):
        return [(r[0], r[1], r[2], r[3]) for r in c.execute(
            "SELECT tc.name, tc.arguments, r.content, tc.state FROM tool_calls tc "
            "JOIN messages a ON a.id = tc.message_id "
            "LEFT JOIN messages r ON r.id = tc.result_message_id "
            "WHERE a.turn_id = ? ORDER BY tc.rowid DESC LIMIT ?", (turn_id, last))][::-1]
    return await store.read(fn)
