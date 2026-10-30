"""Shortening what the model is sent: one-line stubs for old tool results, and the
rolling "earlier in this conversation" summary.

Nothing here writes to the database; turns/context.py decides and persists.
The stored messages and tool results never change: this is only a view.
"""
from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager

import anyio

from .. import prompts
from ..backends.base import BackendError, Done, TextDelta
from . import tokens
from .verify import MSG_REF

log = logging.getLogger("celine.compact")

THINK = re.compile(r"<think>.*?(</think>|$)", re.S)
STUB_HEAD = "[earlier result, shortened to save room]"
MAX_STUB_REFS = 40


def one_line(text: str | None, limit: int = 0) -> str:
    """`text` on one line, cut to `limit` characters with "…" (0: whole)."""
    t = " ".join(str(text or "").split())
    return t if not limit or len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _args(arguments: str | None, limit: int = 300) -> str:
    """Compact JSON of the call's arguments, cut to `limit` characters."""
    try:
        text = json.dumps(json.loads(arguments or "{}"), ensure_ascii=False,
                          separators=(", ", ": "))
    except (json.JSONDecodeError, TypeError):
        text = str(arguments or "")
    return one_line(text, limit)


ARG_TOKENS = 300          # a string argument of an aged call over this is shortened


def short_arguments(arguments: str | None, call_id: str) -> str | None:
    """An aged call's arguments as they go on the wire: every string value over about
    ARG_TOKENS tokens becomes its first line and a mark that says it is shortened and
    how to read the call back; the JSON stays valid. The arguments as they came when
    nothing is that long, or they are not JSON. Byte-identical for the same input: the
    prompt prefix stays cacheable. The stored call never changes."""
    try:
        args = json.loads(arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return arguments
    counter = tokens.Counter()                 # uncalibrated: deterministic
    changed = False

    def short(v):
        nonlocal changed
        if isinstance(v, dict):
            return {k: short(x) for k, x in v.items()}
        if isinstance(v, list):
            return [short(x) for x in v]
        if not isinstance(v, str) or (n := counter.estimate(v, "tool")) <= ARG_TOKENS:
            return v
        changed = True
        first = one_line(v.strip().split("\n", 1)[0], 200)
        return (f"{first} \u2026 [{len(v.splitlines())} lines, about {n:,} tokens, "
                f"shortened; the call is kept: {call_id}; read it back with "
                f"past_conversations__read_tool_call(id=\"{call_id}\")]")
    out = short(args)
    return json.dumps(out, ensure_ascii=False) if changed else arguments


MAX_CITED_LINES = 12


def refs_in(text: str) -> list[str]:
    """The message refs in `text`, in order of first appearance, as "[kind:id]"."""
    out: list[str] = []
    seen: set[str] = set()
    for m in MSG_REF.finditer(text or ""):
        r = f"[{m[1]}:{m[2]}]"
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


_PAGE = re.compile(r"^(throwaway )?tab ")   # skills/browser.py _head: tab, address, title
TAIL_LINES, TAIL_CHARS = 8, 800              # what a terminal's stub keeps of its end


def _tail(lines: list[str]) -> list[str]:
    """The last lines, at most TAIL_LINES and about TAIL_CHARS, each cut to 300."""
    out: list[str] = []
    for ln in reversed(lines[-TAIL_LINES:]):
        if out and sum(map(len, out)) + len(ln) > TAIL_CHARS:
            break
        out.append(one_line(ln, 300))
    return out[::-1]


def stub(name: str, arguments: str | None, content: str | None,
         cited: frozenset[str] | set[str] = frozenset()) -> str:
    """The deterministic one-paragraph stand-in for an old tool result: the call, the
    first lines (a terminal's status line and its last lines: the end is what counts;
    a page's address, title and start), the size, the message refs it showed (so it
    can read them again), and the lines of it it cited since (`cited`: refs in its
    later replies), kept whole. Byte-identical for the same input: the prompt prefix
    stays cacheable."""
    content = content or ""
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if name.startswith("browser__"):
        page = lines[0] if lines and _PAGE.match(lines[0]) and "  " in lines[0] else ""
        start = one_line(" / ".join(lines[1:3] if page else lines[:2]), 300)
        return (f"{STUB_HEAD} {name}({_args(arguments, 120)})"
                + (f" at {page.split('  ', 1)[1]}" if page else "")
                + (f"\nBegan: {start}" if start else ""))
    head = one_line(" / ".join(lines[:1 if name.startswith("shell__") else 2]), 300) \
        if lines else "(empty)"
    refs = refs_in(content)
    n = tokens.Counter().estimate(content, "tool")     # uncalibrated: deterministic
    out = [f"{STUB_HEAD} {name}({_args(arguments)})",
           f"Began: {head}",
           f"About {n:,} tokens."]
    if name.startswith("shell__") and len(lines) > 1:
        out.insert(2, "Ended:\n" + "\n".join(_tail(lines[1:])))
    if refs:
        more = len(refs) - MAX_STUB_REFS
        out.append("Message refs in it: " + " ".join(refs[:MAX_STUB_REFS])
                   + (f" (+{more} more)" if more > 0 else ""))
    if cited:
        kept = [ln for ln in lines if any(r in cited for r in refs_in(ln))]
        if kept:
            more = len(kept) - MAX_CITED_LINES
            out.append("Lines you cited, kept whole:\n" + "\n".join(kept[:MAX_CITED_LINES])
                       + (f"\n(+{more} more cited lines)" if more > 0 else ""))
    out.append("The full text is no longer in view. Call the tool again, or get the "
               "reference with chat_archive__get_messages.")
    return "\n".join(out)


MAX_DROPPED_REFS = 60
DROPPED_HEAD = "Refs in the summarised results that are not mentioned above: "
_DROPPED = re.compile(r"\s*^" + re.escape(DROPPED_HEAD) + r".*$", re.M)


def _dropped_line(summary: str) -> tuple[str, list[str]]:
    """(the summary without its dropped-refs line, the refs that line lists)."""
    m = _DROPPED.search(summary or "")
    if m is None:
        return summary or "", []
    return summary[:m.start()] + summary[m.end():], refs_in(m[0])


def with_dropped_refs(summary: str, items: list[dict], previous: str = "") -> str:
    """The summary plus the refs of the summarised results that it does not mention,
    and those the previous summary listed so, so nothing it found becomes unfindable
    (chat_archive__get_messages reads one)."""
    summary, copied = _dropped_line(summary)
    kept = set(refs_in(summary))
    found = [*_dropped_line(previous)[1], *copied]
    for it in items:
        for c in it.get("calls") or []:
            found += refs_in(c.get("result") or "")
    dropped = [r for r in dict.fromkeys(found) if r not in kept]
    if not dropped:
        return summary
    more = len(dropped) - MAX_DROPPED_REFS
    return (summary.rstrip() + "\n\n" + DROPPED_HEAD + " ".join(dropped[:MAX_DROPPED_REFS])
            + (f" (+{more} more)" if more > 0 else ""))


# ---- the summary (the prompt: turns/compact.md) ------------------------


def transcript(items: list[dict], user: str, name: str) -> list[str]:
    """One block of text per message, tool results as stubs: the summary's input."""
    out: list[str] = []
    for it in items:
        role = it["role"]
        content = (it.get("content") or "").strip()
        atts = it.get("attachments") or []
        if atts:
            content += ("\n" if content else "") + " ".join(
                f"[attached {a.get('filename', 'a file')}]" for a in atts)
        if role == "user":
            out.append(f"{user}: {content}")
            continue
        if role != "assistant":
            continue
        lines = [f"{name}: {content}"] if content else []
        for c in it.get("calls") or []:
            lines.append(f"({name} used {c['name']})\n"
                         + stub(c["name"], c.get("arguments"), c.get("result")))
        if lines:
            out.append("\n".join(lines))
    return out


def chunks(blocks: list[str], budget: int) -> list[str]:
    """Blocks joined into pieces of at most `budget` tokens (a single larger block is
    cut: the summary never needs every character of one message)."""
    out, cur, used = [], [], 0
    for b in blocks:
        n = tokens.estimate(b, "prose")
        if n > budget:
            b, _, _ = tokens.cut_to(b, budget, "prose")
            n = budget
        if cur and used + n > budget:
            out.append("\n\n".join(cur))
            cur, used = [], 0
        cur.append(b)
        used += n
    if cur:
        out.append("\n\n".join(cur))
    return out


class SummaryFailed(Exception):
    """The model gave no summary; the text says why."""


async def summarize(backend, cfg, previous: str, items: list[dict], *, window: int,
                    focus: str | None = None, cap_tokens: int = 3000,
                    input_share: float = 0.5, limit: float = 300.0) -> str:
    """The rolling summary: previous summary + these messages -> one new summary, with
    the conversation's chat model and nothing else (thinking off is the model default).
    Input larger than `input_share` of the window goes in pieces, each folding into the
    summary so far. Raises SummaryFailed with the reason when the model gives nothing
    usable: its error, its time limit, or no text."""
    words = max(150, int(cap_tokens * 0.6 / 1.3))
    system = prompts.fill(cfg, __file__, "compact.md", words=words)
    if focus:
        system += cfg.substitute(f"\n\n{{{{username}}}} asked to focus on: {focus.strip()}"
                                 f". Keep everything about that in detail.")
    model = getattr(backend, "model", None) or "the model"
    try:
        summary = await fold(backend, system, previous,
                             transcript(items, cfg.user, cfg.name), window=window,
                             input_share=input_share, cap=cap_tokens, limit=limit,
                             errors=True)
    except BackendError as e:
        raise SummaryFailed(str(e)) from None
    if not summary:
        raise SummaryFailed(f"{model} gave an empty summary.")
    if tokens.estimate(summary) > cap_tokens:
        summary, _, _ = tokens.cut_to(summary, cap_tokens, "prose")
    return summary


async def fold(backend, system: str, previous: str, lines: list[str], *, window: int,
               input_share: float, cap: int, limit: float = 300.0,
               heads: tuple[str, str] = ("Earlier summary", "Next part of the conversation"),
               errors: bool = False) -> str | None:
    """The earlier text and the next part of `lines`, piece by piece, each folding into
    the text so far. None when the model gives nothing usable; `errors`: a backend
    error is raised (complete)."""
    budget = max(2000, int(window * input_share) - tokens.estimate(system) - cap * 2)
    out = (previous or "").strip()
    for piece in chunks(lines, budget) or [""]:
        body = (f"{heads[0]}:\n{out}\n\n" if out else "") + f"{heads[1]}:\n{piece}"
        text = await complete(backend, [{"role": "system", "content": system},
                                        {"role": "user", "content": body}], limit,
                              errors=errors)
        text = THINK.sub("", text or "").strip()
        if not text:
            return None
        out = text
    return out


async def complete(backend, messages, limit: float, tools=(), token=None,
                   errors: bool = False) -> str | None:
    """One non-streamed completion; text only, tool calls ignored. None on failure,
    when `limit` seconds of the model's own work run out before the reply ends (never
    the part written so far; a wait for the model in its queue does not count), or when
    `token` stops it. `errors`: a backend error, and the time limit as one, is raised
    instead (a work item tells a host that is down from a real error, work.py)."""
    from ..backends.llm import TOOL_CLOCKS
    parts: list[str] = []
    try:
        with anyio.move_on_after(limit) as scope:
            clocks = TOOL_CLOCKS.set((*TOOL_CLOCKS.get(), scope))
            try:
                # a queued backend frees its model slot as soon as we stop reading
                async with closing(stoppable(backend, messages, tools, token)) as events:
                    async for ev in events:
                        if token is not None and token.cancelled:
                            return None
                        if isinstance(ev, TextDelta):
                            parts.append(ev.text)
                        elif isinstance(ev, Done):
                            break
            finally:
                TOOL_CLOCKS.reset(clocks)
        if scope.cancelled_caught:
            raise BackendError(f"{getattr(backend, 'model', None) or 'The model'} did not "
                               f"finish its reply within {limit:.0f} seconds.")
    except BackendError as e:
        log.warning("completion failed", extra={"event": "llm.complete_failed",
                                                "error": str(e)[:200]})
        if errors:
            raise
        return None
    return "".join(parts) or None


def stoppable(backend, messages, tools=(), token=None):
    """The backend's events; a Stop ends the wait for the next one at once. A queued
    backend does that itself (and cancels its HTTP request); a direct one is wrapped."""
    events = backend.stream(messages, list(tools))
    if hasattr(backend, "for_message") or token is None:
        return events
    from .cancel import until_stopped
    return until_stopped(events, token)


@asynccontextmanager
async def closing(events):
    """contextlib.aclosing that also takes an iterator without aclose()."""
    try:
        yield events
    finally:
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            await aclose()
