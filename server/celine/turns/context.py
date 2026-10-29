"""The context builder: what one model request contains.

Principles:
- The database never shrinks for the model's sake. Old tool results are sent as a
  one-line stub (`compact.stub`) and older turns as a rolling summary in the system
  message, but every stored row stays and the UI shows it all.
- The prompt prefix is stable. llama.cpp reuses its KV cache for an unchanged prefix,
  so edits happen rarely, in large steps, and are persisted (`context_state`,
  `compactions`): the same prompt is rebuilt after a restart, byte for byte.
- Decisions are made on the whole request estimate (system + tool schemas + history)
  as a share of the loaded model's window (turns/tokens.py).

The order of what is sent, most stable first:
  system = persona + skill fragments + ["Earlier in this conversation" summary]
  history after the newest compaction: user/assistant messages verbatim (a user
  message under its time line), tool results verbatim or (at or before
  `aged_through`) as stubs.

Modes (who asks):
- "start"      turn start: age above 60 %, compact above 75 % (to 40 %).
- "background" after a turn ended: the same, but compacts above 60 %, so the next turn
               usually starts from a ready summary.
- "turn"       between the steps of one turn when the next request would pass 85 %:
               ages everything but the last 2 results, compacts if still above 75 %.
- "emergency"  the backend said the request does not fit: ages everything but the
               last result, compacts down to the current turn.
- "manual"     /compact: summarises all but the latest exchange, now.
- "dry"        /usage: computes, writes nothing, calls no model.

The tail (`tail`, built per turn, never stored) is appended to the last user-role
message: what holds now, which changes every turn and so stays out of the cached prefix.
It comes in parts (`Part`). What a request leaves out is in `Built.left_out` (the
Context tab), never in its request.

Every message of a request says where it came from (`origin`, the app's key, no backend
sends it): a list of {conversation, turn, message, call, note}, more than one when
consecutive user messages go as one. A stored message names its row, a tool result its
call, what the app adds for this request only its `note`. `malformed` holds a request
to the rules every request keeps: a tool result right after the reply that made its
call, each call answered once, no message twice, nothing from another conversation.

A summary that fails is never covered up: an interactive build (start, turn,
emergency, manual) raises compact.SummaryFailed with the reason; the background one
leaves the messages as they are, says why (`context.compact_failed`) and the next build
tries again. No other model is asked.
"""
from __future__ import annotations

import logging
import weakref
from dataclasses import dataclass, field
from typing import Any

import anyio

from .. import attachments, guard
from ..backends.base import ORIGIN, TAIL
from ..core.ids import when
from ..guard import mark, sources
from ..intercom.render import header
from ..intercom.store import OPENING
from ..clock import zone
from . import compact, compactions, tokens
from ..core.event_types import CONTEXT_COMPACTING, CONTEXT_COMPACT_FAILED

log = logging.getLogger("celine.context")


@dataclass(frozen=True)
class Policy:
    keep_turns: int = 2            # earlier exchanges always verbatim (plus the current)
    age_at: float = 0.60           # request share of the window that triggers aging
    age_to: float = 0.45           # ... aging goes down to this
    compact_at: float = 0.75       # ... that triggers a summary at turn start
    compact_to: float = 0.40       # ... a summary goes down to this
    background_at: float = 0.60    # ... a summary right after a turn
    turn_at: float = 0.85          # ... mid-turn aging
    turn_to: float = 0.30          # ... mid-turn aging goes down to this
    min_edit_tokens: int = 4000    # never break the prompt cache for less
    turn_min_edit_tokens: int = 2000   # ... mid-turn, where every step breaks it anyway
    never_age_below: int = 300     # small results are cheap and often load-bearing
    never_age: tuple[str, ...] = ("notebook__",)
    summary_cap: int = 3000        # tokens of the summary
    summary_input_share: float = 0.5   # of the window, per summary request


POLICY = Policy()


@dataclass(frozen=True)
class Part:
    """One piece of a tail. `rank` 0 always goes; the by-need ones go out in rank order
    when the window is short, the last of a rank first. Consecutive parts with one
    `head` are written under it once.
    `name`: what the Context tab calls it when it is left out. A part with `why` was
    left out before (over its share): never sent, only named, with its `tokens`."""
    text: str
    rank: int = 0
    name: str = ""
    head: str = ""
    why: str = ""
    tokens: int = 0


def render(parts: list[Part]) -> str:
    """The parts as it reads them, a blank line between two."""
    out: list[str] = []
    head = ""
    for p in parts:
        if not p.text or p.why:
            continue
        if p.head and p.head == head:
            out[-1] += "\n\n" + p.text
        else:
            out.append(f"{p.head}\n{p.text}" if p.head else p.text)
        head = p.head
    return "\n\n".join(out)


def room(backend, system: str, tools: list[dict], win: int | None = None) -> int:
    """What a request has for the history and the tail: the window (`win`, else the
    backend's) less the system prompt, the tool schemas and the reserve."""
    counter = tokens.counter(backend)
    win = win or tokens.window(backend)
    return max(0, win - counter.estimate(system) - tokens.PER_MESSAGE
               - (counter.request([], tools) if tools else 0)
               - tokens.max_output(backend) - int(win * tokens.MARGIN))


TRIGGER = {"start": "auto", "background": "background", "turn": "turn",
           "emergency": "emergency", "manual": "manual"}


@dataclass
class Built:
    messages: list[dict]            # system first, then history (wire format)
    estimate: int                   # tokens of the whole request, tool schemas included
    window: int
    aged: int = 0                   # tool results sent as stubs
    compaction: dict | None = None  # the summary in effect
    hidden: list[dict] = field(default_factory=list)   # stored tool results not sent
                                                       # verbatim (citation check)
    actions: list[str] = field(default_factory=list)   # what this build changed
    parts: dict[str, Any] = field(default_factory=dict)  # tokens per part (/usage)
    left_out: list[dict] = field(default_factory=list)   # tail parts with no room:
                                                         # {name, tokens, why}


_locks: weakref.WeakValueDictionary[str, anyio.Lock] = weakref.WeakValueDictionary()


def lock(cid: str) -> anyio.Lock:
    """One context decision per conversation at a time (a turn's build waits for a
    background or manual compaction of the same conversation). A lock no build holds
    goes."""
    return _locks.setdefault(cid, anyio.Lock())


# ---- the time line ------------------------------------------------------------------------
def time_line(m: dict, tz) -> str:
    """"[Fri 2 Oct 01:40]\\n", the first line of every user-role message it reads: the
    stored row stays bare. Empty without a zone or a time."""
    at = m.get("created_at")
    if tz is None or not at:
        return ""
    return f"[{when(at, tz)}]\n"


def origin(cid: str, *, turn: str | None = None, message: str | None = None,
           call: str | None = None, note: str | None = None) -> list[dict]:
    """A request message's origin (module docstring): one entry, its empty fields left
    out."""
    entry = {"conversation": cid, "turn": turn, "message": message, "call": call,
             "note": note}
    return [{k: v for k, v in entry.items() if v is not None}]


def user_wire(cfg, msgs: list[dict], cid: str) -> list[dict]:
    """Messages a turn took at a step boundary (intercom/deliver.py) in conversation
    `cid`, as the context builder sends them: time line, header, text, attachments."""
    tz = zone(cfg)
    guarded = guard.enabled(cfg, cid)
    out: list[dict] = []
    for i, m in enumerate(msgs):
        add_user(out, wire_message(cfg, _Item(i, m, None, tz, cfg.user, guarded=guarded,
                                              cid=cid)))
    return out


def add_user(out: list[dict], wire: list[dict]) -> None:
    """Appends wire messages; consecutive user messages of plain text go as one, a
    blank line between them, with both origins."""
    for w in wire:
        last = out[-1] if out else None
        if (last is not None and w["role"] == last["role"] == "user"
                and isinstance(w["content"], str) and isinstance(last["content"], str)):
            out[-1] = {**last, "content": last["content"] + "\n\n" + w["content"],
                       ORIGIN: [*last.get(ORIGIN, ()), *w.get(ORIGIN, ())]}
        else:
            out.append(w)


def malformed(messages: list[dict], cid: str) -> str | None:
    """What breaks the rules every request keeps (module docstring), as one sentence;
    None when it keeps them. A system message only first; every tool result right after
    the reply that made its call (the results of one reply together, before anything
    else); every call answered once; no stored message twice (a ghost prompt); nothing
    from another conversation; its first message after a user one."""
    calls: set[str] = set()
    answered: set[str] = set()
    open_calls: set[str] = set()
    rows: set[str] = set()
    talked = False
    for i, m in enumerate(messages):
        role = m.get("role")
        for o in m.get(ORIGIN) or ():
            if o.get("conversation") != cid:
                return f"message {i} comes from conversation {o.get('conversation')}"
            if (row := o.get("message")) is not None and role != "tool" and \
                    not o.get("call"):
                if row in rows:
                    return f"message {row} is sent twice"
                rows.add(row)
        if role == "system":
            if i:
                return f"message {i} is a system message after the start"
            continue
        if role == "tool":
            cid_ = m.get("tool_call_id")
            if cid_ in answered:
                return f"call {cid_} is answered twice"
            if cid_ not in open_calls:
                return f"the result of call {cid_} does not follow its call"
            answered.add(cid_)
            open_calls.discard(cid_)
            continue
        if open_calls:
            return f"call {sorted(open_calls)[0]} has no result before message {i}"
        if role == "assistant" and not talked:
            return f"message {i}, an assistant reply, comes before any user message"
        talked = True
        for c in m.get("tool_calls") or ():
            if c["id"] in calls:
                return f"call {c['id']} is made twice"
            calls.add(c["id"])
            open_calls.add(c["id"])
    if open_calls:
        # the request's last reply made calls: it is answered by this request's model
        # only when a prefill continues it, and a prefill never has calls
        return f"call {sorted(open_calls)[0]} has no result"
    return None


# ---- the path as items ----------------------------------------------------------------
HER_FIRST = "This chat starts with your message."   # history: before a reply that is first
THINKING_KEPT = 2             # replies of the current exchange that keep their thinking


def _thought(it: "_Item") -> dict:
    """The agent's thinking on a reply, sent back with it when the reply is `current`
    (_Plan._select): as reasoning_content (a Qwen chat template puts it back inside
    <think>; llama-server and OpenRouter take it) and, for Anthropic, its own signed
    thinking blocks (`thinking`, kept in the reply's meta; a tool loop needs them; a
    reply without a signature has none)."""
    if not it.current:
        return {}
    return {**({"reasoning_content": it.reasoning} if it.reasoning else {}),
            **({"thinking": it.thinking} if it.thinking else {})}


class _Item:
    """One message as it goes on the wire: user text, or an assistant reply with its
    calls and their results. Tool rows of the tree are carried by their call."""
    __slots__ = ("idx", "id", "role", "content", "reasoning", "thinking", "current",
                 "attachments", "calls", "turn", "cid", "_wire", "_cost")

    def __init__(self, idx: int, m: dict, calls: list[dict] | None, tz, user: str = "",
                 bare: bool = False, guarded: bool = False, cid: str = ""):
        self.idx = idx
        self.id = m["id"]
        self.role = m["role"]
        self.turn = m.get("turn_id")
        self.cid = cid
        self.content = m["content"] or ""
        if self.role == "user" and not bare:
            # an intercom line that carries outside text (a mail's sender and subject, a
            # job's result, a sub-agent's report, a host script's line) has it wrapped
            # while the guard is on (guard/sources.py line_label)
            label = sources.line_label(m.get("intercom")) if guarded else None
            if label is not None:
                self.content = mark.wrap_parts(self.content, label)
            self.content = time_line(m, tz) + header(m.get("intercom"), user, tz) \
                + self.content
            if m.get("context"):                # turns/message_context.py
                self.content += "\n\n" + m["context"]
        self.reasoning = (m.get("reasoning") or None) if self.role == "assistant" else None
        self.thinking = m.get("thinking") if self.role == "assistant" else None
        self.current = False                # its thinking goes on the wire (see _select)
        self.attachments = m.get("attachments") or []
        self.calls = calls or None
        self._wire: dict[bool, list[dict]] = {}
        self._cost: dict[bool, int] = {}

    def as_dict(self) -> dict:
        return {"id": self.id, "role": self.role, "content": self.content,
                "attachments": self.attachments, "calls": self.calls}


def _agable(call: dict, policy: Policy, counter) -> bool:
    name = call.get("name") or ""
    if any(name.startswith(p) for p in policy.never_age):
        return False
    if call.get("attachments") or (call.get("display") or {}).get("shot"):
        return True                     # an image goes with its result
    return counter.estimate(call.get("result") or "", "tool") >= policy.never_age_below


class _Plan:
    def __init__(self, view: dict, cfg, backend, counter, policy: Policy, limit: int,
                 tail: list[Part] = (), cid: str = ""):
        self.cfg, self.backend, self.counter, self.policy = cfg, backend, counter, policy
        self.cid = cid
        self.view, self.tail = view, list(tail)
        self.guarded = guard.enabled(cfg, cid)        # a chat can turn Bastet off
        self.dropped: set[int] = set()       # tail parts left out for room
        chain = view["chain"]
        self.chain = chain
        self.pos = {m["id"]: i for i, m in enumerate(chain)}
        tz = zone(cfg)
        items: list[_Item] = []
        for i, m in enumerate(chain):
            if m["role"] == "tool":
                continue                 # emitted with the call that made it
            calls = view["calls"].get(m["id"]) if m["role"] == "assistant" else None
            if not calls and not m["content"] and not m.get("attachments"):
                continue
            items.append(_Item(i, m, calls, tz, cfg.user, guarded=self.guarded, cid=cid))
        self.all_items = items
        comps = view["compactions"]
        self.compaction: dict | None = comps[-1] if comps else None
        self.start = self.pos[self.compaction["through_message_id"]] + 1 \
            if self.compaction else 0
        self.mark = self.pos.get(view["aged"] or "", -1)
        self.limit = limit
        self.opening = ""           # what goes before its first message (history)
        # the refs it cited in its replies: an aged result keeps those lines whole
        self.cited = frozenset(r for it in items if it.role == "assistant"
                               for r in compact.refs_in(it.content))
        self._select()

    def _select(self) -> None:
        """The items after the newest summary, past `limit` cut at a user message, and
        which replies keep their thinking (`current`): the last THINKING_KEPT replies of
        the exchange still going on (measured: the whole exchange's thinking took 44 %
        of a sub-agent's window, and the live loop sends none for older steps anyway),
        and a model's `keep_thinking` earlier exchanges whole. Older exchanges go
        without it, as the templates do."""
        items = [it for it in self.all_items if it.idx >= self.start]
        if self.limit and len(items) > self.limit:     # history_messages, at a user
            items = items[self._cut(items):]
        # an item's age: how many user messages come after it (0: the exchange going on)
        keep = int((getattr(getattr(self.backend, "cfg", None), "raw", None) or {})
                   .get("keep_thinking") or 0)
        age = 0
        recent = 0                    # replies of the exchange going on, newest first
        for it in reversed(items):
            if it.role == "assistant" and age == 0:
                recent += 1
            thinks = age <= keep and (age > 0 or recent <= THINKING_KEPT)
            if it.current != thinks:
                it.current = thinks
                it._wire.clear()
                it._cost.clear()
            if it.role == "user":
                age += 1
        self.items = items

    def cut_through(self, idx: int) -> None:
        """The agent's history starts after chain position `idx` (a summary covers it)."""
        self.start = idx + 1
        self._select()

    def _cut(self, items: list[_Item]) -> int:
        """Where its history starts past `limit` items, always at a user message."""
        cut = len(items) - self.limit
        while cut < len(items) and items[cut].role != "user":
            cut += 1
        return cut

    # -- rendering
    def aged(self, it: _Item) -> bool:
        return bool(it.calls) and it.idx <= self.mark

    def wire(self, it: _Item, aged: bool) -> list[dict]:
        if aged in it._wire:
            return it._wire[aged]
        if it.calls:
            out = [{"role": "assistant", "content": it.content, **_thought(it), "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in it.calls], ORIGIN: origin(it.cid, turn=it.turn, message=it.id)}]
            screens = []
            for c in it.calls:
                came = origin(it.cid, turn=it.turn, message=c.get("result_message_id"),
                              call=c["id"])
                res = c["result"] if c["result"] is not None else "result unavailable"
                stubbed = aged and _agable(c, self.policy, self.counter)
                if not stubbed:
                    res = mark.shown(res, c.get("display"), self.guarded)  # guard/mark.py
                if stubbed:
                    res = compact.stub(c["name"], c["arguments"], c["result"], self.cited)
                elif c.get("attachments"):        # an image the result holds
                    out.append({**attachments.render(self.cfg, [
                        {"role": "tool", "content": res, "attachments": c["attachments"]}])[0],
                        "tool_call_id": c["id"], ORIGIN: came})
                    continue
                out.append({"role": "tool", "tool_call_id": c["id"], "content": res,
                            ORIGIN: came})
                # a result meant to be seen (browser__look's screen, display "see"): the
                # image follows the results of the reply as a user message, until the
                # result is aged away
                shot = (c.get("display") or {}).get("shot") if not stubbed and \
                    (c.get("display") or {}).get("see") else None
                if shot:
                    screens.append({"role": "user", "content": [
                        {"type": "text", "text": f"[the screen from {c['name']}]"},
                        {"type": attachments.IMAGE, "attachment": {
                            **{k: shot[k] for k in ("id", "filename", "sha256",
                                                    "media_type")},
                            "size": attachments.image_size_of(
                                attachments.blob_path(self.cfg, shot["sha256"]))}}],
                        ORIGIN: origin(it.cid, turn=it.turn, call=c["id"], note="screen")})
            out += screens
        else:
            out = wire_message(self.cfg, it)
        it._wire[aged] = out
        return out

    def cost(self, it: _Item, aged: bool | None = None) -> int:
        a = self.aged(it) if aged is None else aged
        if a not in it._cost:
            it._cost[a] = sum(self.counter.message(w) for w in self.wire(it, a))
        return it._cost[a]

    def history(self) -> list[dict]:
        out: list[dict] = []
        for it in self.items:
            add_user(out, self.wire(it, self.aged(it)))
        if out and out[0]["role"] == "assistant" and self.items[0] is self.all_items[0]:
            # its message first (a chat start_chat made): a user line before it, as the
            # chat templates want one, with where it came from
            out.insert(0, {"role": "user", "content": self.opening or HER_FIRST,
                           ORIGIN: origin(self.cid, note="opening")})
        tail = self.tail_text()
        return _with_tail(out, tail, self.cid) if tail else out

    def tail_text(self) -> str:
        return render([p for i, p in enumerate(self.tail) if i not in self.dropped])

    def history_tokens(self) -> int:
        """The history part: the messages after its summary, as they are sent."""
        return sum(self.cost(it) for it in self.items)

    def hidden(self) -> list[dict]:
        """Every stored tool result on the path that is not sent verbatim: shown to
        its earlier, so a citation of it is not 'not shown' (verify.shown_refs)."""
        sent = {it.id for it in self.items if not self.aged(it)}
        out = []
        for it in self.all_items:
            if it.calls and it.id not in sent:
                out.extend({"role": "tool", "content": c["result"]}
                           for c in it.calls if c["result"])
        return out

    # -- structure
    def groups(self) -> list[list[_Item]]:
        """Items split at user messages (one exchange each). Items before the first
        user message (none, normally) form their own group."""
        out: list[list[_Item]] = []
        for it in self.items:
            if it.role == "user" or not out:
                out.append([it])
            else:
                out[-1].append(it)
        return out

    def system(self, base: str) -> str:
        c = self.compaction
        parts = [base] if base else []
        if c is not None and c["text"]:
            parts.append(self.cfg.substitute(
                f"Earlier in this conversation (a summary of {c['covers_messages']} "
                f"older messages; {{{{username}}}} can still see the messages "
                f"themselves):\n{c['text']}"))
        return "\n\n".join(parts)

    def messages(self, base: str) -> list[dict]:
        sys = self.system(base)
        return ([{"role": "system", "content": sys,
                  ORIGIN: origin(self.cid, note="system")}] if sys else []) + self.history()


def wire_message(cfg, it: _Item) -> list[dict]:
    """A message without calls on the wire, with its origin."""
    came = origin(it.cid, turn=it.turn, message=it.id)
    if it.attachments:
        return [{**w, ORIGIN: came} for w in attachments.render(cfg, [
            {"role": it.role, "content": it.content, "attachments": it.attachments}])]
    return [{"role": it.role, "content": it.content, **_thought(it), ORIGIN: came}]


def _with_tail(out: list[dict], tail: str, cid: str) -> list[dict]:
    """The history with the tail as its own content block at the end of the last
    user-role message, or as a user message by itself when its own reply would be last
    (or there is none). add_user never merges across it: it is a list."""
    block = {"type": "text", "text": tail, TAIL: True}
    alone = {"role": "user", "content": [block], ORIGIN: origin(cid, note="tail")}
    if not out or out[-1]["role"] == "assistant" and not out[-1].get("tool_calls"):
        return [*out, alone]
    last = next((i for i in range(len(out) - 1, -1, -1) if out[i]["role"] == "user"), None)
    if last is None:
        return [*out, alone]
    content = out[last]["content"]
    parts = content if isinstance(content, list) else \
        [{"type": "text", "text": content}] if content else []
    return [*out[:last], {**out[last], "content": [*parts, block]}, *out[last + 1:]]


# ---- build --------------------------------------------------------------------------------
async def build(store, cfg, backend, cid: str, *, system: str, tools: list[dict],
                mode: str = "start", focus: str | None = None,
                policy: Policy | None = None, tail: list[Part] = (),
                path: list[dict] | None = None) -> Built:
    """The request for this conversation, after whatever aging or compaction `mode`
    calls for (see the module docstring), with the `tail` parts after the last
    user-role message. `path`: the active path when the turn read it already. A summary
    this build needs and does not get raises compact.SummaryFailed, but in the background
    (module docstring)."""
    policy = policy or POLICY
    if mode == "dry":                  # reads only: never waits for a summary
        return await _build(store, cfg, backend, cid, system, tools, mode, focus, policy,
                            tail, path)
    async with lock(cid):
        return await _build(store, cfg, backend, cid, system, tools, mode, focus, policy,
                            tail, path)


@dataclass
class _Run:
    """One build: what its steps share."""
    store: Any
    cfg: Any
    backend: Any
    cid: str
    plan: "_Plan"
    system: str
    tools: list[dict]
    mode: str
    focus: str | None
    policy: Policy
    counter: Any
    win: int
    before: int = 0
    actions: list[str] = field(default_factory=list)

    @property
    def dry(self) -> bool:
        return self.mode == "dry"

    def used(self) -> int:
        return self.counter.request(self.plan.messages(self.system), self.tools)


@dataclass(frozen=True)
class Rule:
    """When a step of the build acts (`at`, a share of the window) and down to where
    (`to`); what it keeps verbatim: the last `keep` results (or exchanges, for a
    summary), else the last `keep_groups` exchanges; `min`: the fewest tokens worth a
    cache break; `always`: whatever the size (manual, emergency)."""
    at: float
    to: float
    keep: int = 0
    keep_groups: int = 0
    min: int = 0
    always: bool = False


async def _build(store, cfg, backend, cid, system, tools, mode, focus, policy,
                 tail=(), path=None) -> Built:
    counter = tokens.counter(backend)
    win = tokens.window(backend)
    view = await compactions.context_view(store, cid, path)
    plan = _Plan(view, cfg, backend, counter, policy, int(cfg.r("history_messages") or 0),
                 tail, cid)
    plan.opening = await store.read(lambda c: (c.execute(
        "SELECT v FROM kv WHERE k=?", (OPENING + cid,)).fetchone() or [""])[0])
    run = _Run(store, cfg, backend, cid, plan, system, tools, mode, focus, policy,
               counter, win)
    schema = counter.request([], tools) if tools else 0
    run.before = run.used()
    reserve = tokens.reserve(backend)

    # 1. aging: a prefix of old tool results becomes stubs, in one step
    await _age(run)

    # 2. compaction: older exchanges become the rolling summary
    rule = _compact_rule(mode, policy)
    if rule is not None and (rule.always or run.used() > rule.at * win):
        await _compact(run, rule)

    # 3. the recent exchanges alone are too big (a few huge results): shorten all but
    # the last two results, as mid-turn would, rather than overflow
    if mode in ("start", "background", "manual") and run.used() > policy.turn_at * win:
        now_used = run.used()
        new_mark, saved, n = _pick_mark(plan, Rule(0.0, policy.compact_to, keep=2),
                                        now_used, win, policy)
        if new_mark is not None and saved > 0:
            plan.mark = plan.pos[new_mark]
            run.actions.append(f"shortened {n} recent results ({saved:,} tokens)")
            await compactions.set_aged_through(store, cid, new_mark, {
                "results": n, "tokens_saved": saved, "scope": mode,
                "tokens_before": now_used})

    msgs = plan.messages(system)
    est = counter.request(msgs, tools)

    left_out = [{"name": p.name, "why": p.why, "tokens": p.tokens}
                for p in plan.tail if p.why]
    aged_n = sum(1 for it in plan.items if plan.aged(it)
                 for c in it.calls or [] if _agable(c, policy, counter))
    verb = sum(plan.cost(it) for it in plan.items if not plan.aged(it))
    aged_t = sum(plan.cost(it) for it in plan.items if plan.aged(it))
    summary_t = counter.estimate(plan.system(system)) - counter.estimate(system) \
        if plan.compaction else 0
    tail_t = counter.estimate(plan.tail_text())
    parts = {"system": counter.estimate(system) + tokens.PER_MESSAGE,
             "summary": max(0, summary_t), "tool_schemas": schema,
             "tail": tail_t,
             "history_verbatim": verb, "history_aged": aged_t,
             "aged_results": aged_n, "messages_sent": len(msgs),
             "messages_on_version": len(plan.chain),
             "reserve_output": tokens.max_output(backend),
             "margin": int(win * tokens.MARGIN), "estimate": est,
             "free": win - est - reserve}
    return Built(messages=msgs, estimate=est, window=win, aged=aged_n,
                 compaction=plan.compaction, hidden=plan.hidden(), actions=run.actions,
                 parts=parts, left_out=left_out)


async def _age(run: _Run) -> None:
    """A prefix of old tool results becomes stubs, in one step, when the request
    (`run.before` tokens) passes the aging share of the window."""
    plan, win, before = run.plan, run.win, run.before
    age = _age_rule(run.mode, run.policy)
    if age is None or before <= age.at * win:
        return
    new_mark, saved, n = _pick_mark(plan, age, before, win, run.policy)
    if new_mark is None or saved < age.min:
        return
    if run.dry:
        run.actions.append(f"would shorten {n} older results ({saved:,} tokens)")
        return
    plan.mark = plan.pos[new_mark]
    run.actions.append(f"shortened {n} older results ({saved:,} tokens)")
    await compactions.set_aged_through(run.store, run.cid, new_mark, {
        "results": n, "tokens_saved": saved, "scope": run.mode, "tokens_before": before})
    log.info("aged tool results", extra={"event": "context.aged", "mode": run.mode,
                                          "results": n, "saved": saved})


def _age_rule(mode: str, p: Policy) -> Rule | None:
    """keep_groups: exchanges kept verbatim (turn start: the current one plus
    keep_turns; right after a turn there is no current one yet). keep: results kept
    verbatim, counted from the end (mid-turn and emergency)."""
    if mode in ("start", "dry"):
        return Rule(p.age_at, p.age_to, keep_groups=p.keep_turns + 1, min=p.min_edit_tokens)
    if mode == "background":
        return Rule(p.age_at, p.age_to, keep_groups=max(1, p.keep_turns),
                    min=p.min_edit_tokens)
    if mode == "turn":
        # further down than at turn start: results keep coming, and each step here
        # breaks the prompt cache (simulation: 3 breaks in 11 steps at 45 %)
        return Rule(p.turn_at, p.turn_to, keep=2, min=p.turn_min_edit_tokens)
    if mode == "emergency":
        return Rule(0.0, 0.0, keep=1)
    return None                                   # manual: the summary does it


def _compact_rule(mode: str, p: Policy) -> Rule | None:
    """keep: the exchanges a summary keeps verbatim at least."""
    if mode == "start" or mode == "dry":
        return Rule(p.compact_at, p.compact_to, keep=p.keep_turns + 1)
    if mode == "background":
        return Rule(p.background_at, p.compact_to, keep=p.keep_turns)
    if mode == "turn":
        return Rule(p.compact_at, p.compact_to, keep=1)
    if mode == "emergency":
        return Rule(0.0, p.compact_to / 2, keep=1, always=True)
    if mode == "manual":
        return Rule(0.0, p.compact_to, keep=1, always=True)
    return None


def _pick_mark(plan: _Plan, rule: Rule, used: int, win: int,
               policy: Policy) -> tuple[str | None, int, int]:
    """The furthest reply whose results (and every older one) become stubs: the
    shortest prefix that brings the request to `rule.to`, never touching the
    protected tail. -> (message id, tokens saved, results aged)."""
    cands = [it for it in plan.items if it.calls]
    if rule.keep_groups:
        groups = plan.groups()
        keep = max(1, rule.keep_groups)
        cands = [it for it in cands if it.idx < groups[-keep][0].idx] \
            if len(groups) > keep else []
    else:
        # keep the last N results verbatim, counted over replies from the end
        kept, cut = 0, len(cands)
        while cut > 0 and kept < rule.keep:
            cut -= 1
            kept += len(cands[cut].calls or [])
        cands = cands[:cut]
    target = int(rule.to * win)
    mark, saved, n = None, 0, 0
    for it in cands:
        if it.idx <= plan.mark:
            continue
        gain = plan.cost(it, False) - plan.cost(it, True)
        if gain <= 0:
            continue
        mark = it.id
        saved += gain
        n += sum(1 for c in it.calls or [] if _agable(c, policy, plan.counter))
        if used - saved <= target:
            break
    return mark, saved, n


async def _compact(run: _Run, rule: Rule) -> None:
    """Older exchanges into the summary, keeping the newest that fit `rule.to` of the
    window with the system prompt, the tools and the summary itself."""
    plan = run.plan
    groups = plan.groups()
    keep = max(1, rule.keep)
    if len(groups) <= keep:
        return
    fixed = run.counter.request([{"role": "system", "content": run.system}], run.tools) \
        + run.policy.summary_cap
    target = int(rule.to * run.win)

    def size(gs) -> int:
        return sum(plan.cost(it) for g in gs for it in g)

    kept = keep
    if run.mode not in ("manual", "emergency"):   # those keep only the latest exchange
        while kept < len(groups) and fixed + size(groups[-(kept + 1):]) <= target:
            kept += 1
    if kept >= len(groups):
        return
    await _summarise(run, groups[-kept][0].idx - 1)


async def _summarise(run: _Run, through_idx: int) -> None:
    """The plan's history from its start through `through_idx` into its summary, at
    most policy.summary_cap tokens."""
    plan, mode, cap = run.plan, run.mode, run.policy.summary_cap
    if through_idx < plan.start:
        return
    through = plan.chain[through_idx]["id"]
    covered = [it for it in plan.all_items if plan.start <= it.idx <= through_idx]
    covers = sum(1 for m in plan.chain[: through_idx + 1] if m["role"] != "tool")
    trigger = TRIGGER.get(mode, "auto")
    # a summary costs a model call and a cache break: not for a few thousand tokens
    # (simulation: a background summary that saved 3k of 45k). In an emergency any
    # gain is worth it, but a summary longer than what it replaces is not (walkthrough
    # 2: 6,820 tokens before, 6,919 after)
    gain = sum(plan.cost(it) for it in covered if it.idx >= plan.start) - cap // 2
    if mode == "emergency" and gain <= 0:
        return
    if mode not in ("manual", "emergency") and gain < run.policy.min_edit_tokens:
        return
    if run.dry:
        run.actions.append(f"would summarise {len(covered)} messages")
        return
    store, cid = run.store, run.cid
    await store.append_event(cid, CONTEXT_COMPACTING, {
        "trigger": trigger, "messages": len(covered), "through_message_id": through})
    prev = plan.compaction["text"] if plan.compaction else ""
    try:
        text = await compact.summarize(
            run.backend, run.cfg, prev, [it.as_dict() for it in covered],
            window=run.win, focus=run.focus, cap_tokens=cap,
            input_share=run.policy.summary_input_share)
    except compact.SummaryFailed as e:
        # nothing is left out for it: the background build leaves the messages as they
        # are (the next build tries again), every other one fails with the reason
        run.actions.append(f"summary failed: {e}")
        await store.append_event(cid, CONTEXT_COMPACT_FAILED, {
            "trigger": trigger, "messages": len(covered), "through_message_id": through,
            "reason": str(e)})
        log.warning("summary failed", extra={"event": "context.summary_failed",
                                             "trigger": trigger, "error": str(e)})
        if mode == "background":
            return
        raise
    text = compact.with_dropped_refs(text, [it.as_dict() for it in covered], prev)
    old_start = plan.start
    plan.compaction = {"text": text, "covers_messages": covers,
                       "through_message_id": through}
    plan.cut_through(through_idx)
    after = run.used()
    saved = await compactions.save(store, cid, {
        "through_message_id": through, "trigger": trigger,
        "covers_messages": covers, "text": text, "tokens": run.counter.estimate(text),
        "tokens_before": run.before, "tokens_after": after, "focus": run.focus or None,
        "model": getattr(run.backend, "model", None)})
    plan.compaction = {**saved, "through_message_id": through}
    run.actions.append(f"summarised {len(covered)} messages "
                       f"({run.before:,} -> {after:,} tokens)")
    log.info("compacted", extra={"event": "context.compacted", "trigger": trigger,
                                 "messages": len(covered), "before": run.before,
                                 "after": after, "from": old_start})


# ---- entry points besides a turn ---------------------------------------------------------
async def after_turn(store, cfg, backend, cid: str, *, system: str,
                     tools: list[dict]) -> Built | None:
    """Right after a turn, while the model is loaded and the user reads, age and compact
    if the conversation is near the limit, so the next turn starts from a ready prompt.
    Never raises: background work, a failed summary waits for the next build."""
    try:
        return await build(store, cfg, backend, cid, system=system, tools=tools,
                           mode="background")
    except Exception:
        log.exception("background compaction failed",
                      extra={"event": "context.background_failed"})
        return None


def fits(messages: list[dict], tools: list[dict], backend, share: float) -> bool:
    """Whether the next request stays under `share` of the window (estimate)."""
    counter = tokens.counter(backend)
    return counter.request(messages, tools) <= share * tokens.window(backend)
