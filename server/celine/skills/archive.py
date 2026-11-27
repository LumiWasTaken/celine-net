"""The Messages archive: every chat {{username}} ever imported or synced, searchable by
Celine.

Tools read through the running sources (the app's `kbs`). Everything they return that
quotes a message carries its reference, `kb:msg:id`, and it is told to cite with
`[[kb:msg:id]]`, which the transcript renders as a link into the chat. A chat is
`kb:chat:id`, a person `kb:person:id` (kb/identity.py).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

import anyio

from ..kb.identity import chat_ref, kind_of, msg_ref, not_a, parse_msg
from . import ToolContext, ToolResult, guide, knob, tool, described

# Settings, Advanced, read at use (cfg.r). No search or token limit per turn: open
# questions are answered in waves of searches and reads, and the context builder
# (turns/context.py) shortens old results.
KNOBS = (
    knob("chat_archive.search_max_tokens", "Search result length", "tokens", 100,
         10_000_000, 7000, "How long one search result in Messages may be."),
    knob("chat_archive.search_max_tokens_all", "Search result length, all matches",
         "tokens", 100, 10_000_000, 14_000,
         "How long a search result may be when it asks for every match."),
    knob("chat_archive.candidates", "Reranked per search", None, 1, 10_000, 50,
         "How many matches the reranker sorts in one search."),
    knob("chat_archive.rerank_floor", "Lowest reranker score", None, -1000.0, 1000.0, -4.0,
         "A match the reranker scores below this is not about the search. In the model's "
         "score scale: raw logits (default) or 0 to 1 for a model whose Settings, Models "
         "row says its scale is relevance."),
    knob("chat_archive.episode_gap_minutes", "Gap between chat sessions", "minutes", 1,
         100_000, 30, "A longer silence between two messages starts a new chat session."),
    knob("chat_archive.session_max_tokens", "Chat session length", "tokens", 100,
         10_000_000, 4000, "A longer chat session is cut around the message."),
    knob("chat_archive.period_max_tokens", "Date range length", "tokens", 100, 10_000_000,
         40_000, "A date range up to this comes back whole."),
    knob("chat_archive.max_tokens_cap", "Longest read of Messages", "tokens", 100, 10_000_000,
         100_000, "The most one read of Messages may ask for."),
    knob("chat_archive.scroll_max_messages", "Messages per scroll", None, 1, 10_000, 20,
         "How many messages one scroll brings on each side of a message."),
    knob("chat_archive.scroll_max_tokens", "Scroll length", "tokens", 100, 10_000_000,
         4000, "How long one scroll through Messages may be."),
    knob("chat_archive.get_max_refs", "Messages fetched at once", None, 1, 10_000,
         50, "The most found or saved messages one fetch brings."),
)


def _when(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else "?"


def _who(m: dict, me: str) -> str:
    if m.get("from_me"):
        return me
    s = m.get("sender")
    return (s or {}).get("name") or "someone"


def _chat_info(conn, ids):
    from ..kb.chat import chat_cards as info
    return info(conn, ids)


def msg_line(kb: str, m: dict, me: str, mark: bool = False) -> str:
    text = m.get("text") or ""
    if m.get("kind") == "media":
        media = m.get("media") or {}
        text = f"[{media.get('type', 'attachment')}] {text}".strip()
    elif m.get("kind") == "system":
        text = f"({text})"
    text = text.replace("\n", "\n    ")
    return (f"{'>' if mark else ' '} [{msg_ref(kb, m['id'])}] {_when(m['ts'])} "
            f"{_who(m, me)}: {text}")


def _chat_head(title: str, kid: str, chat_id: int, chat: dict) -> str:
    """One chat, the way episode headers name it: source, name, kind, reference."""
    return (f"{title or kid}; {_chat_name(chat)} ({chat.get('kind') or 'chat'}, chat "
            f"{chat_ref(kid, chat_id)})")


def _chat_name(chat: dict | None) -> str:
    c = chat or {}
    return f"{c['parent']} #{c.get('name') or '?'}" if c.get("parent") else c.get("name") or "?"


def _line(kid: str, m, mark: bool = False) -> str:
    """One message with its full date, for results that are not one chat session."""
    from ..kb.retrieve.episodes import clock
    return (f"{'>' if mark else ' '} [{msg_ref(kid, m.id)}] {clock(m.ts):%Y-%m-%d %H:%M} "
            f"{m.sender}: {m.text}")


# ---- scope: where a search looks, decided on purpose ------------------------------------
_chat_counts: dict[str, tuple[float, int]] = {}


def _as_list(v) -> list[str]:
    """A list argument as its entries. A single string is one entry."""
    if not v:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    return [str(x).strip() for x in v if str(x).strip()]


def _n_chats(kb) -> int:
    import time
    hit = _chat_counts.get(kb.id)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    n = int(kb.file.read_sync(lambda c: c.execute(
        "SELECT COUNT(*) FROM chats WHERE message_count > 0").fetchone()[0]))
    _chat_counts[kb.id] = (time.monotonic(), n)
    return n


def _chats_word(n: int) -> str:
    return f"{n:,} chat{'' if n == 1 else 's'}"


def platform_ids(kbs) -> list[str]:
    return [k.id for k in kbs.chats()]


def check_scope(kbs, scope) -> tuple[list[str] | None, str | None]:
    """`scope` as it was passed -> (source ids, None), the ids None for all of Messages;
    or (None, one sentence saying what scope takes). Runs before a search is spent."""
    ids = platform_ids(kbs)
    takes = "scope takes [\"all\"] or source ids: " + ", ".join(f'"{i}"' for i in ids) + "."
    items = _as_list(scope)
    if not items or items == ["all"]:
        return None, None
    bad = []
    for x in items:
        if x in ids:
            continue
        if kind_of(x) == "chat":
            bad.append(f"{x} is a chat: pass it in `chats`.")
        elif kind_of(x):
            bad.append(not_a(x, "chat"))
        else:
            bad.append(f"{x!r} is no source.")
    if bad or "all" in items:
        return None, " ".join(bad + [takes])
    return list(dict.fromkeys(items)), None


def scope_echo(kbs, sources: list[str] | None, resolved) -> tuple[str, dict]:
    """The first result line, saying where the search looked, and its display fields."""
    from ..kb.retrieve.scope import chat_card
    chat_kbs = {k.id: k for k in kbs.chats()}
    if resolved.chats is not None:
        items, cards = [], []
        for kid, ids in sorted(resolved.chats.items()):
            for c in chat_kbs[kid].chats_by_id(ids):
                items.append({"ref": chat_ref(kid, c["id"]), "name": c["name"],
                              "kind": c["kind"], "platform": kid})
                cards.append(chat_card(kid, c))
        n = len(items)
        return (f"scope: {n} chat{'s' if n != 1 else ''}:\n" + "\n".join(
                    f"  {c}" for c in cards),
                {"scope_kind": "chats", "scope_chats": items,
                 "scope_kbs": sorted({i["platform"] for i in items})})
    if sources:
        parts = [f"{p} ({_chats_word(_n_chats(chat_kbs[p]))})" for p in sources]
        word = "source" if len(parts) == 1 else "sources"
        return f"scope: {word} {', '.join(parts)}", {
            "scope_kind": "platforms", "scope_chats": [], "scope_kbs": sources}
    parts = [f"{k} {_chats_word(_n_chats(kb))}" for k, kb in chat_kbs.items()]
    return f"scope: all of Messages ({', '.join(parts)})", {
        "scope_kind": "all", "scope_chats": [], "scope_kbs": sorted(chat_kbs)}


_LAST_NIGHT = re.compile(r"^\s*(?:last night|letzte nacht|gestern (?:nacht|abend)|"
                         r"yesterday (?:night|evening))\s*$", re.IGNORECASE)


_HOURS = re.compile(r"^\s*(\d{1,2})(?::00)?\s*(?:-|\.\.|to|bis)\s*(\d{1,2})(?::00)?\s*$",
                    re.IGNORECASE)


def parse_hours(hours: str | None) -> tuple[set[int] | None, str | None, str | None]:
    """"22-6" -> ({22, 23, 0, ..., 5}, label, None): from the first hour up to, not
    including, the second, wrapping past midnight. (None, None, error) when unreadable."""
    if hours is None or not str(hours).strip():
        return None, None, None
    m = _HOURS.match(str(hours))
    if not m or int(m[1]) > 24 or int(m[2]) > 24:
        return None, None, (f"hours={hours!r} is not a range of hours: pass it like "
                            f"\"22-6\" (22:00 to 05:59) or \"9-17\".")
    a, b = int(m[1]) % 24, int(m[2]) % 24
    span = {(a + i) % 24 for i in range((b - a) % 24 or 24)}
    return span, f"hours: {a:02d}:00 to {(b - 1) % 24:02d}:59 local time", None


def dates_for(when: str | None) -> str | None:
    """Words that name a date through a time of day, as dates: "last night" is
    yesterday and the small hours of today."""
    if when and _LAST_NIGHT.match(str(when)):
        from datetime import date, timedelta
        today = date.today()
        return f"{today - timedelta(days=1)}..{today}"
    return when


def _labels(scope) -> list[str]:
    """The resolution lines after the scope line: when, person (chat lines are in it)."""
    out = []
    for lab in scope.labels:
        if lab.startswith("chat:"):
            continue
        if lab.startswith("person:") and not lab.endswith("→ you"):
            lab += " (only messages they wrote)"
        out.append(lab)
    return out


PEOPLE_HEAD = "[people named]"
PEOPLE_WORDS = 60        # the most words of one message looked up as a name
PEOPLE_LISTED = 5        # the most people listed when a message names several


def _no_match(res, about) -> str:
    """What a search that returns no window says: what was looked for and found
    wanting, as the facts they are."""
    if not _as_list(about):
        return "No message in scope has these words."
    if res.below:
        return (f"No message in scope fits the description: the reranker scored the "
                f"{res.below} closest passages under the lowest score.")
    return "No message in scope fits the description."


class ChatArchive:
    name = "chat_archive"
    guide = guide("chat_archive")
    description = described()

    app = None      # the running app (core/services.py), from skills.build

    def _kbs(self):
        kbs = self.app.kbs if self.app is not None else None
        if kbs is None:
            raise RuntimeError("the knowledge bases are not loaded")
        return kbs

    def __init__(self, cfg):
        self.cfg = cfg
        self.me = cfg.user

    @staticmethod
    def _cap(ctx, n: int) -> int:
        """A tool's own cap, never above the share of the context one result may take."""
        m = ctx.max_output_tokens
        return min(int(n), int(m)) if m else int(n)

    async def prompt_fragment(self, ctx: ToolContext) -> str | None:
        try:
            kbs = self._kbs()
        except RuntimeError:
            return None
        infos = []
        for kb in kbs.all.values():
            info = await anyio.to_thread.run_sync(kb.info)
            if info["kind"] == "chat" and info.get("messages"):
                infos.append(f"- `{info['id']}` ({info['title']}): {info['messages']:,} "
                             f"messages in {info['chats']:,} chats, "
                             f"{_when(info['first_ts'])[:10]} to {_when(info['last_ts'])[:10]}")
        if not infos:
            return None
        return self.cfg.substitute(
            "The Messages archive: {{username}}'s own chats, one source per line. The id "
            "at the start is what `scope` takes:\n" + "\n".join(infos) + "\n\n"
            "You do not remember the archive. What was said, by whom, when and how often "
            "comes from a chat_archive tool call in this turn, never from memory, and "
            "never from shell or files.\n\n"
            "Three kinds of reference, told apart by their shape:\n"
            "- a chat: `discord:chat:18`. It goes in `chats` and in `chat`.\n"
            "- a person: `discord:person:71`. It goes in `written_by`.\n"
            "- a message: `discord:msg:95095`. It goes in `ref` and `refs`, and it is "
            "what you cite.\n"
            "A number alone means nothing: person 71 and chat 71 are different things.\n\n"
            "What each tool is for:\n"
            "- `chat_archive__look_up(name)`: who or what a name is. A person with every "
            "name they have, their profile, what is noted about them, the direct chat "
            "with them, the chats they write in; chats named so.\n"
            "- `chat_archive__write_person(person)`: keeps what you learned about a person "
            "with the person: a note, how they relate to {{username}}, other names.\n"
            "- `chat_archive__list_chats`: the chats of a source, newest first or biggest "
            "first.\n"
            "- `chat_archive__read_period(chat)`: the messages of one chat, the newest "
            "ones or a date range. This is how a chat is read.\n"
            "- `chat_archive__search(about, words)`: messages about a topic, or with exact "
            "words, in all chats, a source, or the chats you name.\n"
            "- `chat_archive__count`: exact numbers: how many, how often, who most, when.\n"
            "- `chat_archive__read_session(ref)`: the chat session around one message.\n"
            "- `chat_archive__scroll(ref)`: the messages before or after one message.\n"
            "- `chat_archive__get_messages(refs)`: messages by reference.\n\n"
            "Every result starts with a `scope:` line that says where it looked.\n\n"
            "Cite a message you use as [[discord:msg:95095]], with the reference a tool "
            "showed you, and say who wrote it. Tell what was said in your own words; quote "
            "exact words only when the wording matters. The app checks citations against "
            "the messages.")

    async def _scope(self, ctx, *, sources, people, chats, when):
        """-> (kbs, the resolved scope, identities). Raises ValueError with what to pass."""
        from ..kb import identity
        from ..kb.retrieve.scope import resolve
        ids = await identity.snapshot(ctx.store)
        kbs = self._kbs()
        when = dates_for(when)
        scope = await anyio.to_thread.run_sync(lambda: resolve(
            kbs, kb_ids=sources, people=_as_list(people) or None,
            chats=_as_list(chats) or None, when=when, identities=ids,
            me_names={self.me}))
        if sources and scope.chats is not None:
            out = sorted(k for k in scope.chats if k not in sources)
            if out:
                raise ValueError(f"chats names a chat of {', '.join(out)}, which scope "
                                 f"leaves out. Leave scope out, or add that source.")
        return kbs, scope, ids

    async def _where(self, ctx, scope, chats, written_by, when):
        """The scope of a search or count -> (kbs, scope, identities, first line, display),
        or a ToolResult saying what to pass instead."""
        try:
            kbs = self._kbs()
        except RuntimeError as e:
            return ToolResult(content=str(e), is_error=True)
        sources, bad = check_scope(kbs, scope)
        if bad:
            return ToolResult(content=bad, is_error=True, display={"scope_error": True})
        try:
            kbs, res, ids = await self._scope(ctx, sources=sources, people=written_by,
                                              chats=chats, when=when)
        except ValueError as e:
            return ToolResult(content=str(e), is_error=True, display={"scope_error": True})
        echo, display = await anyio.to_thread.run_sync(lambda: scope_echo(kbs, sources, res))
        return kbs, res, ids, echo, display

    def _embedder(self):
        """Query embeddings from the first model of the Embeddings task that answers
        (kb/semantic.py). The callable carries that model's fingerprint as `fp`, so an
        index made by another model is skipped."""
        from ..kb import semantic
        try:
            e = semantic.query_embedder(self.cfg)
        except semantic.SemanticError as why:
            return None, "", str(why).rstrip(".")

        def embed(texts):
            return e.embed(texts, max_input_chars=1200)
        embed.fp = e.fp                         # type: ignore[attr-defined]
        return embed, e.s["query_prefix"], None

    # ---- search and count ------------------------------------------------------------
    @tool()
    async def search(
        self, ctx: ToolContext,
        about: Annotated[list[str], "1 to 3 full sentences that describe the messages you "
                                    "want: who, what, what happens. They are compared by "
                                    "meaning, so no word has to match. Add a sentence in "
                                    "the chats' language when it is not English."],
        scope: Annotated[list[str] | None, "The sources to search: source ids such as "
                                           "[\"discord\"]. Leave it out, or pass "
                                           "[\"all\"], for every source."] = None,
        chats: Annotated[list[str] | None, "Search only inside these chats: chat "
                                           "references such as [\"discord:chat:18\"]."] = None,
        words: Annotated[list[str] | None, "Exact words or phrases that someone typed. "
                                           "Several words in one entry are one phrase."] = None,
        written_by: Annotated[list[str] | None, "Only messages these people wrote: person "
                                                "references such as "
                                                "[\"discord:person:71\"], or \"me\". A "
                                                "reference covers every account of that "
                                                "person."] = None,
        when: Annotated[str | None, "A date range: 2021, 2021-03, 2021-03..2021-05, "
                                    "\"last summer\". No time of day."] = None,
        order: Annotated[Literal["best", "first", "last"], "best: best fit first. first: "
                         "oldest first. last: newest first."] = "best",
        mode: Annotated[Literal["best", "all"], "best: the top match windows. all: up to 25 "
                        "match windows, with counts per year."] = "best",
    ) -> ToolResult:
        """Find messages in the Messages archive by what they are about (`about`) and by
        exact words (`words`). Returns: a `scope:` line, the count of each exact word,
        then match windows, best first. A match window is the messages of one chat from
        the first to the last match plus the messages around them, each with its
        reference; `>` marks the lines that matched. Only windows that fit the
        description are returned: when none does, the result says so."""
        from ..kb.retrieve import Settings, retrieve
        from ..kb.retrieve import episodes as E
        from ..kb.retrieve.rerank import from_config
        got = await self._where(ctx, scope, chats, written_by, when)
        if isinstance(got, ToolResult):
            return got
        kbs, scope, _, echo, echo_display = got

        r = self.cfg.r
        titles = {k.id: k.title for k in kbs.pick(None)}

        def search():
            # in the worker thread: choosing the models may ask their servers
            embed, prefix, why = self._embedder()
            settings = Settings(candidates=int(r("chat_archive.candidates")),
                                rerank_floor=float(r("chat_archive.rerank_floor")),
                                episode_gap_minutes=int(r("chat_archive.episode_gap_minutes")),
                                query_prefix=prefix)
            return retrieve(kbs, _as_list(words), scope, about=_as_list(about),
                            order=order, mode=mode, me=self.me, embed=embed, embed_why=why,
                            reranker=from_config(self.cfg), settings=settings)
        try:
            res = await anyio.to_thread.run_sync(search)
        except ValueError as e:
            return ToolResult(content=str(e), is_error=True)

        head = [echo] + _labels(scope)
        if res.totals:
            head.append("exact word matches in scope: " + ", ".join(
                f"{q} {n:,}" for q, n in res.totals.items()))
        if res.years:
            head.append("per year: " + ", ".join(f"{y} {n:,}" for y, n in res.years.items()))
        head += res.notes
        cap = int(r("chat_archive.search_max_tokens_all" if mode == "all"
                    else "chat_archive.search_max_tokens"))
        cap = self._cap(ctx, cap)
        shown, cut, used = [], 0, 0
        for e in res.episodes:
            text = E.render(e, len(shown) + 1, kb_title=titles.get(e.kb, e.kb))
            t = E.tokens(text)
            if shown and used + t > cap:
                cut += 1
                continue
            used += t
            shown.append((e, text))
        body = []
        if shown:
            what = {"first": "earliest first", "last": "most recent first"}.get(order,
                                                                                 "best first")
            body.append(f"{len(shown)} match window{'s' if len(shown) != 1 else ''} "
                        f"({what}). > marks the matching lines:")
            body += [t for _, t in shown]
        else:
            body.append(_no_match(res, about))
        tail = []
        if cut or res.more:
            tail.append(f"{cut + res.more} more match windows fit and are not shown: one "
                        f"result holds {cap:,} tokens. `when`, `chats` or `written_by` "
                        f"narrow the search; mode=\"all\" returns more of them.")
        content = "\n".join(head + [""] + ["\n\n".join(body)] + ([""] + tail if tail else []))
        display = {"episodes": [{"kb": e.kb, "chat": e.chat_id,
                                 "chat_name": (e.chat or {}).get("name"),
                                 "start_ts": e.lines[0].ts if e.lines else e.start_ts,
                                 "end_ts": e.lines[-1].ts if e.lines else e.end_ts,
                                 "refs": e.refs, "hits": sorted(f"{e.kb}:{h}" for h in e.hits
                                                                if h in {m.id for m in e.lines})}
                                for e, _ in shown],
                   "totals": res.totals, "absent": res.absent, "below": res.below,
                   "scope": scope.labels,
                   **echo_display,
                   "reranked": res.reranked, "took_ms": res.took_ms}
        return ToolResult(content=content.strip(), display=display)

    @tool()
    async def count(
        self, ctx: ToolContext,
        by: Annotated[Literal["total", "year", "month", "weekday", "hour", "person", "chat",
                              "co_participant"],
                      "The grouping: total, year, month, weekday, hour, person, chat, or "
                      "co_participant (everyone who wrote within 30 minutes of a match in "
                      "the same chat)."],
        scope: Annotated[list[str] | None, "The sources to count in: source ids such as "
                                           "[\"discord\"]. Leave it out, or pass "
                                           "[\"all\"], for every source."] = None,
        chats: Annotated[list[str] | None, "Count only inside these chats: chat "
                                           "references such as [\"discord:chat:18\"]."] = None,
        words: Annotated[list[str] | None, "Exact words or phrases that someone typed. A "
                                           "word also matches as the start of a longer "
                                           "word. Leave it out to count every "
                                           "message."] = None,
        written_by: Annotated[list[str] | None, "Only messages these people wrote: person "
                                                "references such as "
                                                "[\"discord:person:71\"], or \"me\"."] = None,
        when: Annotated[str | None, "A date range: 2021, 2021-03, 2021-03..2021-05, "
                                    "\"last summer\". No time of day."] = None,
        hours: Annotated[str | None, "Only messages sent in these local hours: \"22-6\" is "
                                     "22:00 to 05:59."] = None,
        unit: Annotated[Literal["messages", "days", "match_windows"], "What one counts: "
                        "messages, days (distinct days), or match_windows (matching "
                        "messages of one chat less than 30 minutes apart count as "
                        "one)."] = "messages",
    ) -> ToolResult:
        """Count messages in the Messages archive exactly. Returns: the `scope:` line, the
        total, and one row per group (a year, a person, a chat) with its count; a person
        or chat row carries its reference."""
        from ..kb.retrieve.count import count as do_count
        words = [q for q in _as_list(words) if q.strip()] or None
        hour_set, hour_label, bad = parse_hours(hours)
        if bad:
            return ToolResult(content=bad, is_error=True)
        got = await self._where(ctx, scope, chats, written_by, when)
        if isinstance(got, ToolResult):
            return got
        kbs, scope, ids, echo, echo_display = got
        if hour_set is not None:
            scope.hours = hour_set
            scope.labels.append(hour_label)
        try:
            res = await anyio.to_thread.run_sync(lambda: do_count(
                kbs, scope, by=by, queries=words, unit=unit, me=self.me,
                identities=ids))
        except ValueError as e:
            return ToolResult(content=str(e), is_error=True)
        what = f" matching {' or '.join(repr(q) for q in words)}" if words else ""
        lines = [echo] + _labels(scope)
        noun = unit.replace("_", " ")
        lines.append(f"{res.total:,} {noun}{what}" + (f", by {by}:" if by != "total" else ""))
        if by == "co_participant":
            lines[-1] = (f"{res.total:,} {noun}{what}. Who wrote within 30 minutes of them "
                         f"({noun} each person was around for):")
        if by != "total":
            for r in res.rows:
                refs = f" ({', '.join(r['refs'])})" if r["refs"] else ""
                lines.append(f"  {r['label']}{refs}: {r['count']:,}")
            if not res.rows:
                lines.append("  (none)")
        return ToolResult(content="\n".join(lines),
                          display={"total": res.total, "rows": len(res.rows), "unit": unit,
                                   **echo_display})

    async def message_context(self, ctx: ToolContext, message: dict):
        """`[people named]`: the card of the person a new user message names by a name,
        a username or an alias (kb/cards.py); with several people named, one line each.
        Only a whole word that is exactly a name counts. (text, report) or None."""
        from ..kb import cards, identity
        from ..kb.query import resolve_people
        from ..kb.retrieve.scope import person_card
        from .places import offered, place_of
        from .recall import _STOP
        store = ctx.store

        def facts(c):
            conv = c.execute("SELECT auto_context FROM conversations WHERE id=?",
                             (ctx.conversation_id,)).fetchone()
            return conv and bool(conv[0]) and offered(
                "chat_archive", place_of(c, ctx.conversation_id, ctx.turn_id))
        if not await store.read(facts):
            return None
        # a username has digits, dots and underscores: shiniloli_2, moon.child
        words = [w for w in dict.fromkeys(
                     x.strip(".") for x in re.findall(r"[\w.]+", message.get("content") or ""))
                 if len(w) >= 3 and w.casefold() not in _STOP][:PEOPLE_WORDS]
        if not words:
            return None
        ids = await identity.snapshot(store)
        kbs = self._kbs()

        def run():
            chat_kbs = kbs.chats()
            found: dict[str, tuple[str, dict]] = {}       # person -> (the word, candidate)
            for w in words:
                res = resolve_people(chat_kbs, w, identities=ids)
                if res.match == "exact":
                    for c in res.candidates:
                        found.setdefault(c["ref"], (w, c))
            if len(found) == 1:
                [ref] = found
                return found, cards.person(kbs, ids, ref, self.me)
            accts = identity.accounts(kbs, list(found)[:PEOPLE_LISTED])
            return found, [f"{person_card(a)}, named {found[r][0]!r}"
                           for r, a in accts.items()]
        found, lines = await anyio.to_thread.run_sync(run)
        if not lines:
            return None
        if len(found) > PEOPLE_LISTED:
            lines.append(f"{len(found) - PEOPLE_LISTED} more people match a word of the "
                         f"message.")
        return ("\n".join([PEOPLE_HEAD, *lines]),
                {"people": [{"ref": r, "word": w} for r, (w, _) in found.items()]})

    # ---- people and chats -------------------------------------------------------------
    @tool()
    async def look_up(
        self, ctx: ToolContext,
        name: Annotated[str, "A name, the start of a name, a username, a nickname, or "
                             "digits of a phone number."],
    ) -> ToolResult:
        """Who or what a name is, on every source. Returns: for each person, a `person
        <ref>` line per account with every name it has and what each name is, their
        profile (bio, pronouns), the names they had before, how they relate to
        {{username}}, the notes about them, the direct chat with them and the other chats
        they write in; then `chat <ref>` lines of the chats named so. Two people can have
        the same name: their other names and message counts tell them apart."""
        from ..kb import cards, identity
        from ..kb.query import resolve_chats, resolve_people
        from ..kb.retrieve.scope import chat_card
        ids = await identity.snapshot(ctx.store)
        kbs = self._kbs()
        chat_kbs = kbs.chats()

        def run():
            people = resolve_people(chat_kbs, name, identities=ids)
            blocks = [b for c in people.candidates[:10]
                      if (b := cards.person(kbs, ids, c["ref"], self.me))]
            found = resolve_chats(chat_kbs, name)
            rows = [(k.id, c) for k in chat_kbs for c in k.chats_by_id(
                [c["id"] for c in found.candidates if c["kb"] == k.id])]
            rows.sort(key=lambda kc: -kc[1]["messages"])
            return people, blocks, found, rows
        people, blocks, found, chats = await anyio.to_thread.run_sync(run)
        how = {"exact": "", "prefix": ", by the start of a name",
               "fuzzy": ", by a close spelling"}
        lines: list[str] = []
        if blocks:
            n = len(people.candidates)
            lines.append(f"{n} {'person' if n == 1 else 'people'} matching {name!r}"
                         f"{how.get(people.match, '')}:")
            for block in blocks:
                lines += [f"  {ln}" for ln in block]
            if n > len(blocks):
                lines.append(f"  {n - len(blocks)} more not listed. Pass a longer name.")
        if chats:
            lines.append(f"{len(chats)} chat{'' if len(chats) == 1 else 's'} named like "
                         f"{name!r}{how.get(found.match, '')}:")
            lines += ["  " + chat_card(k, c) for k, c in chats[:10]]
        if not lines:
            lines.append(f"No person and no chat matches {name!r}. A shorter part of the "
                         f"name, a username or digits of the number may match.")
        return ToolResult(content="\n".join(lines),
                          display={"people": len(people.candidates), "chats": len(chats)})

    @tool(read_only=False, destructive=False)
    async def write_person(
        self, ctx: ToolContext,
        person: Annotated[str, "A person reference, such as discord:person:71."],
        note: Annotated[str | None, "One thing you learned about them, in a sentence: "
                                    "\"moved to Hamburg in 2025\"."] = None,
        relation: Annotated[str | None, "How they relate to {{username}}, in a few words: "
                                        "\"sister\", \"friend from school\". It replaces "
                                        "the one that stands."] = None,
        aliases: Annotated[list[str] | None, "Other names {{username}} calls them: "
                                             "[\"Sis\"]. They are added to the ones "
                                             "that stand."] = None,
    ) -> ToolResult:
        """Keep what you learned about a person with the person: a note, how they relate
        to {{username}}, the other names they go by. It is on their card from then on, in
        every conversation. Returns: the person's card as it stands now."""
        from ..kb import cards, identity
        store = ctx.store
        if store is None:
            return ToolResult(content="No database here.", is_error=True)
        kbs = self._kbs()
        try:
            kid, pid = identity.parse_person(person)
        except identity.IdentityError as e:
            return ToolResult(content=str(e), is_error=True)
        kb = kbs.get(kid)
        if not hasattr(kb, "person_name") or await anyio.to_thread.run_sync(
                kb.person_name, pid) is None:
            return ToolResult(content=f"There is no person {person}.", is_error=True)
        if not (note or relation or aliases):
            return ToolResult(content="Pass a note, a relation or aliases.", is_error=True)

        def write(c):
            if relation is not None or aliases:
                rec = identity.set_person_sync(c, person)
                have = rec["aliases"]
                identity.set_person_sync(
                    c, person, relation=relation,
                    aliases=[*have, *_as_list(aliases)] if aliases else None)
            if note:
                identity.note_sync(c, person, note, author="agent")
        try:
            await store.write(write)
        except identity.IdentityError as e:
            return ToolResult(content=str(e), is_error=True)
        ids = await identity.snapshot(store)
        card = await anyio.to_thread.run_sync(
            lambda: cards.person(kbs, ids, person, self.me, chats=False))
        return ToolResult(content="Kept.\n" + "\n".join(card), display={"person": person})

    @tool()
    async def list_chats(
        self, ctx: ToolContext,
        source: Annotated[str | None, "A source id such as \"discord\". Leave it out for "
                                      "every source."] = None,
        kind: Annotated[Literal["dm", "group", "channel"] | None, "Only direct chats (dm), "
                        "groups, or server channels."] = None,
        sort: Annotated[Literal["recent", "messages", "name"], "recent: last active first. "
                        "messages: most messages first. name: by name."] = "recent",
        limit: Annotated[int, "How many chats, 1 to 100."] = 30,
    ) -> ToolResult:
        """List the chats in the Messages archive. Returns: one `chat <ref>` line per
        chat with its name, its kind (a direct chat names the person on the other side),
        how many messages it has and its first and last day; then how many more there
        are."""
        from ..kb.retrieve.scope import chat_card
        kbs = self._kbs()
        ids = platform_ids(kbs)
        if source is not None and source not in ids:
            return ToolResult(content=f"{source!r} is no source. source takes one of: "
                                      + ", ".join(f'"{i}"' for i in ids) + ".", is_error=True)
        limit = max(1, min(int(limit or 30), 100))
        order = {"messages": "count"}.get(sort, sort)

        def run():
            rows, total = [], 0
            for kb in kbs.chats():
                if source in (None, kb.id):
                    got = kb.list_chats(sort=order, kind=kind, limit=limit)
                    rows += [(kb.id, c) for c in got["chats"]]
                    total += got["total"]
            key = {"recent": lambda kc: -(kc[1]["last_ts"] or 0),
                   "count": lambda kc: -kc[1]["messages"],
                   "name": lambda kc: (kc[1]["name"] or "").casefold()}[order]
            return sorted(rows, key=key)[:limit], total
        rows, total = await anyio.to_thread.run_sync(run)
        where = source or "all of Messages"
        what = {"dm": "direct chat", "group": "group", "channel": "channel"}.get(kind, "chat")
        head = (f"{len(rows)} of {total:,} {what}s in {where}, "
                + {"recent": "last active first", "count": "most messages first",
                   "name": "by name"}[order] + ":")
        if not rows:
            return ToolResult(content=f"No {what}s in {where}.", display={"chats": 0})
        return ToolResult(
            content="\n".join([head] + ["  " + chat_card(k, c) for k, c in rows]),
            display={"chats": len(rows), "total": total, "scope_kind": "chats",
                     "scope_chats": [{"ref": chat_ref(k, c["id"]), "name": c["name"],
                                      "kind": c["kind"], "platform": k} for k, c in rows],
                     "scope_kbs": sorted({k for k, _ in rows})})

    @tool(read_only=False, destructive=False)
    async def mark_same_person(
        self, ctx: ToolContext,
        accounts: Annotated[list[str], "Two or more person references, such as "
                                       "[\"whatsapp:person:17\", \"discord:person:71\"]."],
        name: Annotated[str, "The name for the person."],
    ) -> ToolResult:
        """Mark several accounts as one person. Only after {{username}} confirms it: two
        equal names are no proof. After this, `written_by`, counts and look-ups treat the
        accounts as one person. Returns: the person's name and its accounts."""
        from ..kb import identity
        store = ctx.store
        if store is None:
            return ToolResult(content="No database here.", is_error=True)
        accounts = _as_list(accounts)
        if len(accounts) < 2:
            return ToolResult(content="accounts takes two or more person references, such "
                                      "as whatsapp:person:17.", is_error=True)
        try:
            out = await store.write(lambda c: identity.link_sync(c, accounts, name=name,
                                                                 source="celine"))
        except identity.IdentityError as e:
            return ToolResult(content=str(e), is_error=True)
        return ToolResult(content=f"Marked as the same person, '{out['name']}': "
                                  f"{', '.join(out['members'])}",
                          display={"identity": out["id"]})

    # ---- reading ---------------------------------------------------------------------
    @tool()
    async def read_session(
        self, ctx: ToolContext,
        ref: Annotated[str, "A message reference, such as whatsapp:msg:48213."],
        max_tokens: Annotated[int | None, "The most text you want back."] = None,
    ) -> ToolResult:
        """Read the chat session that contains one message, in order. A chat session is
        the messages of one chat between two pauses longer than the session gap (90
        minutes unless Settings says otherwise). Returns: the chat, the session's
        messages with their references, the message you passed marked `>`, then the
        references just before and after what is shown. A session longer than
        `max_tokens` comes as the messages around yours, and says how many it has."""
        return await self._read_ref(ctx, ref, max_tokens)

    @tool()
    async def read_period(
        self, ctx: ToolContext,
        chat: Annotated[str, "A chat reference, such as whatsapp:chat:213."],
        when: Annotated[str | None, "A date range: 2021-03, 2021-03-01..2021-03-14, \"last "
                                    "summer\", \"the last 3 months\". Leave it out for "
                                    "the newest messages."] = None,
        max_tokens: Annotated[int | None, "The most text you want back."] = None,
    ) -> ToolResult:
        """Read one chat: its newest messages, or the messages of a date range, in order.
        Returns: the chat, then the messages with their references, then the references
        just before and after what is shown. Without `when`, as many of the newest
        messages as fit. A date range longer than `max_tokens` comes as one line per chat
        session (when, how many messages, who, its longest message) to read further
        with chat_archive__read_session."""
        return await self._read_slice(ctx, chat, when, max_tokens)

    @tool()
    async def get_messages(
        self, ctx: ToolContext,
        refs: Annotated[list[str], "One or more message references, such as "
                                   "[\"whatsapp:msg:48213\"]."],
    ) -> ToolResult:
        """Get messages by their references. A reference never changes, so one you saved
        earlier still works. Returns: the messages grouped by chat, each with sender and
        time, then the references that are no message."""
        from ..kb.chat import ChatKB
        from ..kb.retrieve import episodes as E
        cap = int(self.cfg.r("chat_archive.get_max_refs"))
        items = [r.strip().strip("[]") for r in _as_list(refs)]
        items = list(dict.fromkeys(r for r in items if r))
        if not items:
            return ToolResult(content="refs takes message references, such as "
                                      "whatsapp:msg:48213.", is_error=True)
        extra = items[cap:]
        items = items[:cap]
        kbs = self._kbs()
        wanted: dict[str, list[int]] = {}
        bad: list[str] = []
        for r in items:
            m = parse_msg(r)
            if m and isinstance(kbs.get(m[0]), ChatKB):
                wanted.setdefault(m[0], []).append(m[1])
            else:
                bad.append(r)

        def fetch(kb, ids):
            def run(c):
                q = ",".join("?" * len(ids))
                rows = c.execute(f"{E.SELECT} WHERE m.id IN ({q}) ORDER BY m.ts, m.id",
                                 ids).fetchall()
                msgs = [E.to_msg(r, self.me) for r in rows]
                return msgs, _chat_info(c, {x.chat_id for x in msgs})
            return kb.file.read_sync(run)

        blocks, chats, found = [], [], set()
        for kid, ids in wanted.items():
            kb = kbs.get(kid)
            msgs, info = await anyio.to_thread.run_sync(lambda: fetch(kb, ids))
            by_chat: dict[int, list] = {}
            for x in msgs:
                by_chat.setdefault(x.chat_id, []).append(x)
                found.add(msg_ref(kid, x.id))
            for cid, xs in by_chat.items():
                ch = info.get(cid) or {}
                chats.append({"ref": chat_ref(kid, cid), "name": ch.get("name") or "?",
                              "kind": ch.get("kind") or "chat", "platform": kid})
                blocks.append("\n".join(filter(None, [
                    _chat_head(kb.title, kid, cid, ch), E.who(kid, xs),
                    *(_line(kid, x) for x in xs)])))
        missing = [r for r in items if r not in found and r not in bad]
        tail = []
        if bad:
            tail += [not_a(r, "message") for r in bad]
        if missing:
            tail.append(f"no such message: {', '.join(missing)}")
        if extra:
            tail.append(f"{len(extra)} more references not returned. One call takes {cap}.")
        n = len(found)
        head = f"{n} message{'s' if n != 1 else ''} in {len(chats)} chat" \
               f"{'s' if len(chats) != 1 else ''}:" if n else "No messages found."
        content = "\n\n".join([head] + blocks + (["\n".join(tail)] if tail else []))
        return ToolResult(content=content, display={"messages": n, "missing": missing + bad,
                                   "scope_kind": "chats", "scope_chats": chats,
                                   "scope_kbs": sorted({c["platform"] for c in chats})})

    @tool()
    async def scroll(
        self, ctx: ToolContext,
        ref: Annotated[str, "The message to scroll from: a message reference, such as "
                            "whatsapp:msg:48213."],
        direction: Annotated[Literal["before", "after", "both"], "before: older messages. "
                             "after: newer messages. both: each side."] = "both",
        count: Annotated[int, "Messages per side, 1 to 20."] = 20,
    ) -> ToolResult:
        """Get the messages before or after one message in its chat, up to 20 a side.
        Returns: the chat, the messages oldest first with the one you passed marked `>`,
        the range they cover, and the call that scrolls further or that the start or
        the newest message of the chat is reached."""
        from ..kb.chat import ChatKB
        from ..kb.retrieve import episodes as E
        most = int(self.cfg.r("chat_archive.scroll_max_messages"))
        per_side = max(1, min(int(count or most), most))
        budget = self._cap(ctx, int(self.cfg.r("chat_archive.scroll_max_tokens")))
        m = parse_msg(str(ref or ""))
        kb = self._kbs().get(m[0]) if m else None
        if not isinstance(kb, ChatKB):
            return ToolResult(content=not_a(str(ref), "message"), is_error=True)
        kid, mid = m
        if direction not in ("before", "after", "both"):
            return ToolResult(content="direction is before, after or both.", is_error=True)
        sides = ["before", "after"] if direction == "both" else [direction]
        side_budget = budget // len(sides)

        def run(c):
            a = c.execute(f"{E.SELECT} WHERE m.id=?", (mid,)).fetchone()
            if a is None:
                return None
            anchor = E.to_msg(a, self.me)
            out = {"anchor": anchor, "info": _chat_info(c, {anchor.chat_id})}
            for side in sides:
                op, order = ("<", "DESC") if side == "before" else (">", "ASC")
                rows = c.execute(
                    f"{E.SELECT} WHERE m.chat_id=? AND (m.ts, m.id) {op} (?, ?) "
                    f"ORDER BY m.ts {order}, m.id {order} LIMIT ?",
                    (anchor.chat_id, anchor.ts, anchor.id, per_side + 1)).fetchall()
                got, used = [], 0
                for r in rows[:per_side]:
                    x = E.to_msg(r, self.me)
                    t = E.tokens(x.text) + 9
                    if got and used + t > side_budget:
                        break
                    used += t
                    got.append(x)
                # the edge of the chat: nothing past what it returns
                end = len(got) == len(rows)
                if side == "before":
                    got.reverse()
                out[side] = (got, end)
            return out
        got = await anyio.to_thread.run_sync(lambda: kb.file.read_sync(run))
        if got is None:
            return ToolResult(content=f"There is no message {msg_ref(kid, mid)}.",
                              is_error=True)
        anchor, chat_id = got["anchor"], got["anchor"].chat_id
        ch = got["info"].get(chat_id) or {}
        before = got.get("before", ([], True))[0]
        after = got.get("after", ([], True))[0]
        seq = [*before, anchor, *after]

        body = list(filter(None, [E.who(kid, seq)]))
        prev_day = None
        for x in seq:
            t = E.clock(x.ts)
            stamp = f"{t:%H:%M}" if t.date() == prev_day else f"{t:%Y-%m-%d %H:%M}"
            prev_day = t.date()
            mark = ">" if x.id == anchor.id else " "
            body.append(f"{mark} [{msg_ref(kid, x.id)}] {stamp} {x.sender}: {x.text}")

        first, last = seq[0], seq[-1]
        a_t, b_t = E.clock(first.ts), E.clock(last.ts)
        covered = (f"these are {msg_ref(kid, first.id)} ({a_t:%Y-%m-%d %H:%M}) to "
                   f"{msg_ref(kid, last.id)} ({b_t:%Y-%m-%d %H:%M}) of this chat")
        nxt = []
        if "before" in got:
            nxt.append("before: start of the chat reached" if got["before"][1] else
                       f"older: chat_archive__scroll(ref=\"{msg_ref(kid, first.id)}\", "
                       f"direction=\"before\")")
        if "after" in got:
            nxt.append("after: newest message reached" if got["after"][1] else
                       f"newer: chat_archive__scroll(ref=\"{msg_ref(kid, last.id)}\", "
                       f"direction=\"after\")")
        parts = []
        if "before" in got:
            parts.append(f"{len(before)} before")
        if "after" in got:
            parts.append(f"{len(after)} after")
        head = [await self._chat_line(kb, chat_id),
                _chat_head(kb.title, kid, chat_id, ch),
                f"{len(before) + len(after)} messages around {msg_ref(kid, anchor.id)} "
                f"({', '.join(parts)}), oldest first:"]
        content = "\n".join(head + body + ["", covered + ".", "; ".join(nxt)])
        return ToolResult(content=content, display={
            "messages": len(before) + len(after), "anchor": msg_ref(kid, anchor.id),
            "direction": direction,
            "first": msg_ref(kid, first.id), "last": msg_ref(kid, last.id),
            "scope_kind": "chats", "scope_chats": [{
                "ref": chat_ref(kid, chat_id), "name": ch.get("name") or "?",
                "kind": ch.get("kind") or "chat", "platform": kid}],
            "scope_kbs": [kid]})

    @staticmethod
    def _handles(kb, chat_id: int, first: tuple[int, int] | None,
                 last: tuple[int, int] | None) -> tuple[str, dict]:
        """The references just outside what was shown, to page like scrolling. Written
        without brackets: they were not shown, so they do not count as citable."""
        def run(c):
            e = c.execute("SELECT id FROM messages WHERE chat_id=? AND (ts, id) < (?, ?) "
                          "ORDER BY ts DESC, id DESC LIMIT 1",
                          (chat_id, *first)).fetchone() if first else None
            l_ = c.execute("SELECT id FROM messages WHERE chat_id=? AND (ts, id) > (?, ?) "
                           "ORDER BY ts, id LIMIT 1", (chat_id, *last)).fetchone() if last else None
            return (e[0] if e else None), (l_[0] if l_ else None)
        e, l_ = kb.file.read_sync(run)
        earlier = msg_ref(kb.id, e) if e else None
        later = msg_ref(kb.id, l_) if l_ else None
        line = (f"earlier: {earlier or 'none (start of the chat)'}; "
                f"later: {later or 'none (newest message)'}")
        return line, {"earlier": earlier, "later": later}

    async def _chat_line(self, kb, chat_id: int) -> str:
        """The `scope:` line of a result that reads one chat."""
        from ..kb.retrieve.scope import chat_card
        rows = await anyio.to_thread.run_sync(kb.chats_by_id, [chat_id])
        return "scope: 1 chat:\n  " + (chat_card(kb.id, rows[0]) if rows
                                       else f"chat {chat_ref(kb.id, chat_id)}")

    async def _read_ref(self, ctx, ref: str, max_tokens: int | None) -> ToolResult:
        from ..kb.chat import ChatKB
        from ..kb.retrieve import episodes as E
        parsed = parse_msg(str(ref or ""))
        kb = self._kbs().get(parsed[0]) if parsed else None
        if not isinstance(kb, ChatKB):
            return ToolResult(content=not_a(str(ref), "message"), is_error=True)
        kid, mid = parsed
        m = await anyio.to_thread.run_sync(kb.message, mid)
        if m is None:
            return ToolResult(content=f"There is no message {msg_ref(kid, mid)}.",
                              is_error=True)
        chat = m["chat"] or {}
        chat_id = int(chat.get("id"))
        cap = int(self.cfg.r("chat_archive.max_tokens_cap"))
        budget = min(int(max_tokens or self.cfg.r("chat_archive.session_max_tokens")), cap)
        budget = self._cap(ctx, budget)
        msgs, total = await anyio.to_thread.run_sync(
            lambda: self._session_around(kb, chat_id, m, budget))
        info = await anyio.to_thread.run_sync(
            lambda: kb.file.read_sync(lambda c: _chat_info(c, {chat_id})))
        ep = E.Episode(kid, chat_id, msgs[0].ts, msgs[-1].ts, (msgs[0].ts, msgs[0].id),
                       (msgs[-1].ts, msgs[-1].id), 0.0, hits={m["id"]}, lines=msgs,
                       chat=info.get(chat_id) or chat)
        handles, hd = await anyio.to_thread.run_sync(lambda: self._handles(
            kb, chat_id, (msgs[0].ts, msgs[0].id), (msgs[-1].ts, msgs[-1].id)))
        head = [await self._chat_line(kb, chat_id)]
        if total > len(msgs):
            head.append(f"its chat session has {total:,} messages. {len(msgs):,} around it "
                        f"shown.")
        else:
            head.append(f"its whole chat session, {total:,} messages")
        body = "\n".join(head) + "\n" + E.render(ep, kb_title=kb.title) + "\n" + handles
        return ToolResult(content=body, display={"chat": chat.get("name"),
                                                 "messages": len(msgs), "scope_kind": "chats",
                                                 "scope_chats": [{"ref": chat_ref(kid, chat_id),
                                                                  "name": (ep.chat or chat).get("name"),
                                                                  "kind": (ep.chat or chat).get("kind"),
                                                                  "platform": kid}],
                                                 **hd})

    def _session_around(self, kb, chat_id: int, m: dict, budget: int):
        """The chat session the message belongs to, trimmed around it to the budget: from
        the semantic topic search index's sessions when the message is in one, else cut
        here by the same configured pause rule (the session gap, default 90 minutes). ->
        (messages shown, session size)."""
        import sqlite3

        from ..kb import sessions as S
        from ..kb.retrieve import episodes as E

        def run(c):
            rows = None
            try:
                sid = c.execute("SELECT session_id FROM session_members WHERE message_id=?",
                                (m["id"],)).fetchone()
            except sqlite3.OperationalError:     # no semantic index built yet: no tables
                sid = None
            if sid:
                se = c.execute("SELECT first_id, last_id, start_ts, end_ts FROM sessions "
                               "WHERE id=?", (sid[0],)).fetchone()
                if se:
                    rows = c.execute(
                        f"{E.SELECT} WHERE m.chat_id=? AND (m.ts, m.id) BETWEEN (?, ?) "
                        f"AND (?, ?) ORDER BY m.ts, m.id LIMIT 3000",
                        (chat_id, se[2], se[0], se[3], se[1])).fetchall()
            if not rows:
                older = c.execute(f"{E.SELECT} WHERE m.chat_id=? AND (m.ts, m.id) < (?, ?) "
                                  f"ORDER BY m.ts DESC, m.id DESC LIMIT 400",
                                  (chat_id, m["ts"], m["id"])).fetchall()[::-1]
                me_ = c.execute(f"{E.SELECT} WHERE m.id=?", (m["id"],)).fetchall()
                newer = c.execute(f"{E.SELECT} WHERE m.chat_id=? AND (m.ts, m.id) > (?, ?) "
                                  f"ORDER BY m.ts, m.id LIMIT 400",
                                  (chat_id, m["ts"], m["id"])).fetchall()
                allr = [*older, *me_, *newer]
                gap = S.SessionParams.from_settings(self.cfg.embeddings).gap_ms
                for part in S.cut([(r["id"], r["ts"], 0) for r in allr], gap):
                    if any(x[0] == m["id"] for x in part):
                        keep = {x[0] for x in part}
                        rows = [r for r in allr if r["id"] in keep]
                        break
            return [E.to_msg(r, self.me) for r in rows or []]
        msgs = kb.file.read_sync(run)
        idx = next((i for i, x in enumerate(msgs) if x.id == m["id"]), 0)
        lo = hi = idx
        used = E.tokens(msgs[idx].text) + 9 if msgs else 0
        while True:
            grew = False
            for side in (-1, 1):
                j = lo - 1 if side < 0 else hi + 1
                if 0 <= j < len(msgs):
                    t = E.tokens(msgs[j].text) + 9
                    if used + t <= budget:
                        used += t
                        lo, hi = (j, hi) if side < 0 else (lo, j)
                        grew = True
            if not grew:
                break
        return msgs[lo:hi + 1], len(msgs)

    async def _read_slice(self, ctx, chat: str, when: str | None,
                          max_tokens: int | None) -> ToolResult:
        from ..kb.retrieve import episodes as E
        from ..kb import identity
        from ..kb.retrieve.scope import resolve_chat
        from ..kb.retrieve.when import parse_when
        cap = int(self.cfg.r("chat_archive.max_tokens_cap"))
        budget = min(int(max_tokens or self.cfg.r("chat_archive.period_max_tokens")), cap)
        budget = self._cap(ctx, budget)
        kbs = self._kbs()
        ids = await identity.snapshot(ctx.store)
        try:
            kid, cid = await anyio.to_thread.run_sync(
                lambda: resolve_chat(kbs.chats(), chat, ids, "chat"))
            rng = parse_when(dates_for(when))
        except ValueError as e:
            return ToolResult(content=str(e), is_error=True)
        kb = kbs.get(kid)
        label = chat_ref(kid, cid)
        info = await anyio.to_thread.run_sync(
            lambda: kb.file.read_sync(lambda c: _chat_info(c, {cid})))
        lo, hi = (rng[0], rng[1]) if rng else (None, None)

        def run(conn):
            w, args = "m.chat_id = ?", [cid]
            if lo is not None:
                w += " AND m.ts >= ? AND m.ts < ?"
                args += [lo, hi]
            n, chars = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(LENGTH(m.text)), 0) "
                                    f"FROM messages m WHERE {w}", args).fetchone()
            est = E.tokens("x" * int(chars)) + n * 9
            if n == 0:
                return n, est, [], None
            if est > budget and lo is not None:
                return n, est, [], self._digest(conn, w, args, budget, kid)
            # everything, or (no `when`) the newest messages that fit
            rows = conn.execute(f"{E.SELECT} WHERE {w} ORDER BY m.ts DESC, m.id DESC LIMIT ?",
                                [*args, 20_000]).fetchall()
            out, used = [], 0
            for r in rows:
                msg = E.to_msg(r, self.me)
                used += E.tokens(msg.text) + 9
                if used > budget:
                    break
                out.append(msg)
            return n, est, out[::-1], None
        n, est, msgs, digest = await anyio.to_thread.run_sync(lambda: kb.file.read_sync(run))
        span = rng[2] if rng else "newest"
        chat_row = info[cid]
        scope_line = await self._chat_line(kb, cid)
        disp = {"chat": label, "scope_kind": "chats",
                "scope_chats": [{"ref": label, "name": chat_row.get("name"),
                                 "kind": chat_row.get("kind"), "platform": kid}]}
        if n == 0:
            return ToolResult(content=f"{scope_line}\n{label}: no messages in {span}",
                              display={**disp, "messages": 0})
        if not msgs:
            lines, first, last = digest
            handles, hd = await anyio.to_thread.run_sync(
                lambda: self._handles(kb, cid, first, last))
            return ToolResult(
                content=(f"{scope_line}\n{label}, {span}: {n:,} messages, ~{est // 1000}k "
                         f"tokens, more than fits ({budget:,}). One line per chat session: "
                         f"when, how many messages, who, and one line of it with its "
                         f"reference. Read a chat session with chat_archive__read_session "
                         f"before you quote from it.\n" + "\n".join(lines) + "\n" + handles),
                display={**disp, "messages": n, "tokens": est, "digest": True, **hd})
        ep = E.Episode(kid, cid, msgs[0].ts, msgs[-1].ts, (0, 0), (0, 0), 0.0, lines=msgs)
        ep.chat = chat_row
        handles, hd = await anyio.to_thread.run_sync(lambda: self._handles(
            kb, cid, (msgs[0].ts, msgs[0].id), (msgs[-1].ts, msgs[-1].id)))
        head = (f"{label}, {span}: all {n:,} messages" if len(msgs) == n else
                f"{label}, {span}: the newest {len(msgs):,} of {n:,} messages that fit")
        return ToolResult(content=scope_line + "\n" + head + "\n" +
                          E.render(ep, kb_title=kb.title) + "\n" + handles,
                          display={**disp, "messages": len(msgs), **hd})

    def _digest(self, conn, where: str, args: list, budget: int, kid: str):
        """A long range as one line per chat session (cut at the session gap, by default
        90 minutes): start,
        size, who, and its longest message, whole, with its reference. An index to read
        further with read_session."""
        from ..kb import sessions as S
        from ..kb.retrieve import episodes as E
        rows = conn.execute(f"{E.SELECT} WHERE {where} ORDER BY m.ts, m.id LIMIT 60000",
                            args).fetchall()
        msgs = [E.to_msg(r, self.me) for r in rows]
        parts = S.cut([(i, x.ts, len(x.text)) for i, x in enumerate(msgs)],
                      S.SessionParams.from_settings(self.cfg.embeddings).gap_ms)
        lines, used = [], 0
        shown_first = shown_last = None
        step = 1
        # too many chat sessions for the budget: show every k-th, and say so
        est_line = 30
        if len(parts) * est_line > budget:
            step = max(1, -(-len(parts) * est_line // budget))
        for k in range(0, len(parts), step):
            part = [msgs[i] for i, _, _ in parts[k]]
            longest = max(part, key=lambda x: len(x.text))
            who: dict[str, int] = {}
            for x in part:
                who[x.sender] = who.get(x.sender, 0) + 1
            top = ", ".join(n for n, _ in sorted(who.items(), key=lambda kv: -kv[1])[:3])
            a = datetime.fromtimestamp(part[0].ts / 1000)
            b = datetime.fromtimestamp(part[-1].ts / 1000)
            span = f"{a:%Y-%m-%d %H:%M} to {b:%H:%M}" if a.date() == b.date() else \
                f"{a:%Y-%m-%d %H:%M} to {b:%m-%d %H:%M}"
            line = (f"  {span}; {len(part)} msgs; {top}; [{msg_ref(kid, longest.id)}] "
                    f"{longest.sender}: {longest.text}")
            t = E.tokens(line)
            if used + t > budget:
                lines.append(f"  … {len(range(k, len(parts), step))} more chat sessions "
                             f"not listed. Narrow `when`.")
                break
            used += t
            lines.append(line)
            shown_first = shown_first or (part[0].ts, part[0].id)
            shown_last = (part[-1].ts, part[-1].id)
        if step > 1:
            lines.insert(0, f"  (every {step}th of {len(parts):,} chat sessions)")
        return lines, shown_first, shown_last
