"""The Messages archive: every chat {{username}} ever imported or synced, searchable by
Celine.

Tools read through the running sources (the app's `kbs`). Everything they return that
quotes a message carries its reference, `kb:id`, and it is told to cite with
`[[kb:id]]`, which the transcript renders as a link into the chat.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

import anyio

from ..kb.identity import REF
from . import ToolContext, ToolResult, guide, knob, tool, described

# Settings, Advanced, read at use (cfg.r). No search or token limit per turn: open
# questions are answered in waves of searches and reads, and the context builder
# (turns/context.py) shortens old results.
KNOBS = (
    knob("chat_archive.search_max_tokens", "Search result length", "tokens", 100,
         10_000_000, 7000),
    knob("chat_archive.search_max_tokens_all", "Search result length, all matches",
         "tokens", 100, 10_000_000, 14_000),
    knob("chat_archive.candidates", "Reranked per search", None, 1, 10_000, 50),
    knob("chat_archive.rerank_floor", "Rerank floor", None, -1000.0, 1000.0, -4.0,
         "A match the reranker scores below this is not about the search."),
    knob("chat_archive.episode_gap_minutes", "Episode gap", "minutes", 1, 100_000, 30,
         "A longer silence starts a new episode."),
    knob("chat_archive.session_max_tokens", "Session length", "tokens", 100, 10_000_000,
         4000, "A longer session is cut around the message."),
    knob("chat_archive.period_max_tokens", "Date range length", "tokens", 100, 10_000_000,
         40_000, "A date range up to this comes back whole."),
    knob("chat_archive.max_tokens_cap", "Longest read", "tokens", 100, 10_000_000,
         100_000, "The most one read may ask for."),
    knob("chat_archive.scroll_max_messages", "Messages per scroll", None, 1, 10_000, 20,
         "On each side, in one call."),
    knob("chat_archive.scroll_max_tokens", "Scroll length", "tokens", 100, 10_000_000,
         4000),
    knob("chat_archive.get_max_refs", "Messages per lookup", None, 1, 10_000, 50,
         "By reference, in one call."),
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
    return f"{'>' if mark else ' '} [{kb}:{m['id']}] {_when(m['ts'])} {_who(m, me)}: {text}"


def _chat_head(title: str, kid: str, chat_id: int, chat: dict) -> str:
    """One chat, the way episode headers name it: source, name, kind, reference."""
    return (f"{title or kid}; {_chat_name(chat)} ({chat.get('kind') or 'chat'}, chat "
            f"{kid}:{chat_id})")


def _chat_name(chat: dict | None) -> str:
    c = chat or {}
    return f"{c['parent']} #{c.get('name') or '?'}" if c.get("parent") else c.get("name") or "?"


def _line(kid: str, m, mark: bool = False) -> str:
    """One message with its full date, for results that are not one chat session."""
    from ..kb.retrieve.episodes import clock
    return f"{'>' if mark else ' '} [{kid}:{m.id}] {clock(m.ts):%Y-%m-%d %H:%M} {m.sender}: {m.text}"


def _person_chats(kb, person_id: int, limit: int = 8) -> list[dict]:
    """The chats one account writes in, most messages first, with how many it wrote
    there and when last."""
    def run(c):
        rows = c.execute("SELECT chat_id, COUNT(*), MAX(ts) FROM messages WHERE sender_id=? "
                         "GROUP BY chat_id ORDER BY COUNT(*) DESC LIMIT ?",
                         (person_id, limit)).fetchall()
        info = _chat_info(c, {r[0] for r in rows})
        return [{**info.get(r[0], {}), "id": r[0], "n": r[1], "last_ts": r[2]} for r in rows]
    return kb.file.read_sync(run)


def _their_chats(kbs, refs, limit: int = 8) -> list[str]:
    """`chat <ref>` lines for the chats these accounts (one person) write in, most
    messages first."""
    rows = []
    for ref in sorted(refs):
        kid, _, pid = ref.partition(":")
        kb = kbs.get(kid)
        if hasattr(kb, "list_chats") and pid.isdigit():
            rows += [(kid, ch) for ch in _person_chats(kb, int(pid), limit)]
    merged: dict[str, dict] = {}            # two accounts of one person in one chat
    for kid, ch in rows:
        m = merged.setdefault(f"{kid}:{ch['id']}", {**ch, "n": 0, "last_ts": 0})
        m["n"] += ch["n"]
        m["last_ts"] = max(m["last_ts"], ch["last_ts"] or 0)
    top = sorted(merged.items(), key=lambda kv: -kv[1]["n"])[:limit]
    return [f"chat {ref} {_chat_name(ch)} ({ch.get('kind') or 'chat'}), {ch['n']:,} by "
            f"them, last {_when(ch['last_ts'])[:7]}" for ref, ch in top]


# ---- scope: where a search looks, decided on purpose ------------------------------------
_ALL = {"all", "everything", "*"}
_chat_counts: dict[str, tuple[float, int]] = {}


def _as_list(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
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


def check_scope(kbs, scope) -> tuple[str, list[str], list[str], str | None]:
    """`scope` as it passed it -> (kind, platform ids, chats, None), kind "all",
    "platforms" or "chats"; or ("", [], [], one sentence saying what to pass instead).
    Runs before any search is spent."""
    ids = platform_ids(kbs)
    platforms: dict[str, str] = {}
    for k in kbs.chats():
        for word in (k.id, getattr(k, "title", ""), getattr(k, "platform", "")):
            if word:
                platforms[str(word)] = k.id
    example = (f'["all"], ["{ids[0]}"] or chat references from chat_archive__look_up '
               f'like ["{ids[0]}:213"]') if ids else '["all"]'
    items = _as_list(scope)
    if not items:
        return "", [], [], f"scope is required: {example}."
    if any(x in _ALL for x in items):
        return "all", [], [], None
    plats = [platforms[x] for x in items if x in platforms and not REF.match(x)]
    chats = [x for x in items if x not in platforms or REF.match(x)]
    if plats and chats:
        return "", [], [], (f"scope mixes sources ({', '.join(plats)}) and chats "
                            f"({', '.join(chats)}). Pass one kind: the source ids, or the "
                            f"chat references alone.")
    if plats:
        return "platforms", sorted(set(plats), key=plats.index), [], None
    return "chats", [], chats, None


def missing_chats(echo_display: dict) -> str | None:
    """A chat reference that names no chat: often a message or person reference."""
    bad = [i["ref"] for i in echo_display.get("scope_chats", []) if i.get("missing")]
    if not bad:
        return None
    return (f"{', '.join(bad)} {'is' if len(bad) == 1 else 'are'} not a chat. scope takes "
            f"chat references, the `chat ...` lines of chat_archive__look_up. Message and "
            f"account references look the same but are not chats.")


def scope_echo(kbs, kind: str, platform_ids: list[str], resolved) -> tuple[str, dict]:
    """The first result line, saying where the search looked, and its display fields."""
    chat_kbs = {k.id: k for k in kbs.chats()}
    if kind == "chats":
        items = []
        for kid, ids in sorted((resolved.chats or {}).items()):
            kb = chat_kbs.get(kid)
            if kb is None or not ids:
                continue
            info = kb.file.read_sync(lambda c, ids=ids: _chat_info(c, set(ids)))
            for cid in sorted(ids):
                ch = info.get(cid)
                item = {"ref": f"{kid}:{cid}", "name": (ch or {}).get("name") or "?",
                        "kind": (ch or {}).get("kind") or "chat", "platform": kid}
                if ch is None:
                    item["missing"] = True
                items.append(item)
        line = (f"scope: {len(items)} chat{'s' if len(items) != 1 else ''}: " + ", ".join(
            f"{i['ref']} \"{i['name']}\" ({i['kind']})" for i in items[:12])
            + (f" and {len(items) - 12} more" if len(items) > 12 else ""))
        return line, {"scope_kind": "chats", "scope_chats": items, "scope_kbs": sorted(
            {i["platform"] for i in items})}
    if kind == "platforms":
        parts = [f"{p} ({_chats_word(_n_chats(chat_kbs[p]))})" for p in platform_ids
                 if p in chat_kbs]
        word = "source" if len(parts) == 1 else "sources"
        return f"scope: {word} {', '.join(parts)}", {
            "scope_kind": "platforms", "scope_chats": [], "scope_kbs": platform_ids}
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

    def _precheck(self, scope) -> tuple[str, list[str], list[str], str | None]:
        """A scope that cannot be searched, said before a search is spent.
        -> (kind, source ids, chats, None) or ("", [], [], what to pass instead)."""
        try:
            kbs = self._kbs()
        except RuntimeError as e:
            return "", [], [], str(e)
        return check_scope(kbs, scope)

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
            "The Messages archive: {{username}}'s own chats, one source per line. These ids "
            "are what `scope` takes for one source:\n" + "\n".join(infos) + "\n\n"
            "You do not remember the archive. Every question about it (what was said, by "
            "whom, when, how often, who someone is) starts with a chat_archive tool call "
            "in this turn, and the answer comes only from what the tools showed you. "
            "Never answer it from memory, and never write that you searched when no tool "
            "ran. Use the chat_archive tools only, never shell or files. "
            "`chat_archive__search` finds what and when. `chat_archive__count` answers how "
            "many and who most. `chat_archive__look_up` turns a name into references: pass "
            "people and chats as references, never as names. A name you do not know (a "
            "nickname, an alias) is most likely someone in the archive: look it up with "
            "`chat_archive__look_up` before you ask {{username}} who it is. "
            "`chat_archive__read_session` reads the chat session around a message. "
            "`chat_archive__read_period` reads one chat over a date range. "
            "`chat_archive__scroll` gets the messages above or below a message. "
            "`chat_archive__get_messages` gets messages by reference, also references you "
            "kept or found in an earlier conversation.\n\n"
            "`chat_archive__search` runs a semantic topic search on `about`: full sentences "
            "that describe the messages you want (\"Noor tells {{username}} what she likes to "
            "cook\"). That finds what nobody named with a keyword. `words` is only for "
            "exact words someone typed. Never pass keyword lists as `about`.\n\n"
            "Answer open questions (what someone likes, wants, thinks, how something went, "
            "\"search deeply\") in waves, with many calls: "
            "(1) several searches with different `about` sentences, in the chats' languages, "
            "from different angles. (2) Read the best match windows in full with "
            "`chat_archive__read_session`, and scroll with `chat_archive__scroll` where it "
            "gets interesting. (3) Narrow down with what you read: search inside the chat "
            "that keeps coming up, the people involved, the period. (4) Look for the "
            "opposite or exceptions. (5) Answer when new steps bring nothing new. There is "
            "no limit on searches. Depth means exploring what you found, not searching all "
            "chats again with other words. A closed question (a date, a count) takes one "
            "or two calls.\n\n"
            "`scope` is where you look, decided on purpose: [\"all\"], a source id, or "
            "chat references from `chat_archive__look_up` when {{username}} names a chat, a "
            "group or \"my chats with X\". A person often has one direct message per source: "
            "pass each. `written_by` only filters who wrote. It does not choose chats. "
            "Every result starts with `scope:` (and `when:`) lines. If they are not what "
            "you meant, search again. When you change direction while searching (narrowing "
            "to a chat or person, scrolling around a match), write one short plain line "
            "before the calls (\"nothing in the group, trying your DM with Noor\"). Never "
            "write filler. In your answer, say once in plain words where you looked (\"I "
            "searched all your chats\", \"only your DM with Noor on WhatsApp\").\n\n"
            "Every message line has a reference like `whatsapp:48213`. Cite what you use "
            "as [[whatsapp:48213]]: only references a tool showed you. Say who wrote it. "
            "Tell what was said in your own words: the citation is the proof. Quote exact "
            "words only when the wording itself matters. The app checks your answer "
            "against the messages.")

    async def _scope(self, ctx, *, kb, people, chats, when):
        from ..kb import identity
        from ..kb.retrieve.scope import resolve
        ids = await identity.snapshot(ctx.store)
        kbs = self._kbs()
        when = dates_for(when)
        scope = await anyio.to_thread.run_sync(lambda: resolve(
            kbs, kb_ids=kb, people=people, chats=chats, when=when, identities=ids,
            me_names={self.me}))
        return kbs, scope, ids

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

    # ---- the tools to use first ------------------------------------------------------
    @tool()
    async def search(
        self, ctx: ToolContext,
        about: Annotated[list[str], "1 to 3 full sentences that describe a topic or "
                                    "situation: who, what, what happens. They are compared "
                                    "by embedding, so no word has to match. A bare name or "
                                    "\"X is a person\" describes nothing and matches "
                                    "noise: a name goes in `words`. Add a sentence "
                                    "in the chats' language when it is not English."],
        scope: Annotated[list[str], "Where to search: [\"all\"], source ids such as "
                                    "[\"discord\"], or chat references from "
                                    "chat_archive__look_up. Do not mix source ids and chat "
                                    "references."],
        words: Annotated[list[str] | None, "Exact words or phrases that people typed. "
                                           "Several words form one phrase. Leave it empty "
                                           "when no telling word exists."] = None,
        written_by: Annotated[list[str] | None, "Only messages from these accounts: "
                                                "`person` references from "
                                                "chat_archive__look_up, or \"me\". Never "
                                                "names. A reference covers every account "
                                                "marked as the same person. This is for "
                                                "what X said; for what others said about "
                                                "X, pass words=[X's name]."] = None,
        when: Annotated[str | None, "A date range: 2021, 2021-03, 2021-03..2021-05, "
                                    "\"last summer\". No time of day."] = None,
        order: Annotated[Literal["best", "first", "last"], "best: best fit first. first: "
                         "oldest first. last: newest first."] = "best",
        mode: Annotated[Literal["best", "all"], "best: the top match windows. all: up to 25 "
                        "shorter match windows, with counts per year."] = "best",
    ) -> ToolResult:
        """Search the Messages archive: {{username}}'s stored WhatsApp, Discord and imported
        chats. Two searches run at once: semantic topic search on `about`, and word search
        on `words`. A reranker sorts the results. A result is a match window: the messages
        of one chat from the first to the last match, plus up to 2 messages around. Use it
        for what was said, when, and every time. For numbers use chat_archive__count. For
        a name use chat_archive__look_up first. One call is one search: for an open
        question, make many calls. Returns: a `scope:` line, word counts, the match windows
        with references."""
        from ..kb.retrieve import Ambiguous, Settings, retrieve
        from ..kb.retrieve import episodes as E
        from ..kb.retrieve.rerank import from_config
        scope_kind, plats, chats, bad = self._precheck(scope)
        if bad:
            return ToolResult(content=bad, is_error=True, display={"scope_error": True})
        try:
            kbs, res_scope, _ = await self._scope(ctx, kb=plats or None, people=written_by,
                                                  chats=chats or None, when=when)
            echo, echo_display = await anyio.to_thread.run_sync(
                lambda: scope_echo(kbs, scope_kind, plats, res_scope))
            if missing := missing_chats(echo_display):
                raise ValueError(missing)
        except Ambiguous as e:
            return ToolResult(content="\n".join(e.lines), display={"ambiguous": True})
        except ValueError as e:
            return ToolResult(content=str(e), is_error=True)
        scope = res_scope

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
            body.append("No match in scope. Try other words (the other language, a "
                        "synonym, a shorter form), a wider `when`, fewer filters, or "
                        "chat_archive__count with the same words to check whether they "
                        "occur at all.")
        tail = []
        if cut or res.more:
            tail.append(f"{cut + res.more} more not shown. Narrow with when, scope or "
                        f"written_by, or use mode=\"all\".")
        content = "\n".join(head + [""] + ["\n\n".join(body)] + ([""] + tail if tail else []))
        display = {"episodes": [{"kb": e.kb, "chat": e.chat_id,
                                 "chat_name": (e.chat or {}).get("name"),
                                 "start_ts": e.lines[0].ts if e.lines else e.start_ts,
                                 "end_ts": e.lines[-1].ts if e.lines else e.end_ts,
                                 "refs": e.refs, "hits": sorted(f"{e.kb}:{h}" for h in e.hits
                                                                if h in {m.id for m in e.lines})}
                                for e, _ in shown],
                   "totals": res.totals, "scope": scope.labels,
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
        scope: Annotated[list[str], "Where to count: [\"all\"], source ids such as "
                                    "[\"discord\"], or chat references from "
                                    "chat_archive__look_up. Do not mix source ids and chat "
                                    "references."],
        words: Annotated[list[str] | None, "Exact words or phrases that people typed. A "
                                           "word also matches as a prefix. Leave it empty "
                                           "to count every message."] = None,
        written_by: Annotated[list[str] | None, "Only messages from these accounts: "
                                                "account references from "
                                                "chat_archive__look_up, or \"me\". Never "
                                                "names. A reference covers every account "
                                                "marked as the same person."] = None,
        when: Annotated[str | None, "A date range: 2021, 2021-03, 2021-03..2021-05, "
                                    "\"last summer\". No time of day."] = None,
        hours: Annotated[str | None, "Only messages sent in these local hours: \"22-6\" is "
                                     "22:00 to 05:59."] = None,
        unit: Annotated[Literal["messages", "days", "match_windows"], "What one counts: "
                        "messages, days (distinct days), or match_windows (matching "
                        "messages of one chat less than 30 minutes apart count as "
                        "one)."] = "messages",
    ) -> ToolResult:
        """Count messages in the Messages archive exactly, with SQL. Group by year, month,
        weekday, hour, person, chat or co-participant. Use it for how many, how often, when
        most, who most, and did I ever. Never count search results yourself. Returns: the
        `scope:` line, the total, one row per group with references."""
        from ..kb.retrieve import Ambiguous
        from ..kb.retrieve.count import count as do_count
        scope_kind, plats, chats, bad = self._precheck(scope)
        if bad:
            return ToolResult(content=bad, is_error=True, display={"scope_error": True})
        words = [q for q in _as_list(words) if q.strip()] or None
        hour_set, hour_label, bad = parse_hours(hours)
        if bad:
            return ToolResult(content=bad, is_error=True)
        try:
            kbs, scope, ids = await self._scope(ctx, kb=plats or None, people=written_by,
                                                chats=chats or None, when=when)
            if hour_set is not None:
                scope.hours = hour_set
                scope.labels.append(hour_label)
            echo, echo_display = await anyio.to_thread.run_sync(
                lambda: scope_echo(kbs, scope_kind, plats, scope))
            if missing := missing_chats(echo_display):
                raise ValueError(missing)
            res = await anyio.to_thread.run_sync(lambda: do_count(
                kbs, scope, by=by, queries=words, unit=unit, me=self.me,
                identities=ids))
        except Ambiguous as e:
            return ToolResult(content="\n".join(e.lines), display={"ambiguous": True})
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

    @tool()
    async def look_up(
        self, ctx: ToolContext,
        name: Annotated[str, "A name, part of a name, a nickname, or digits of a phone "
                             "number."],
    ) -> ToolResult:
        """Look up a person or a chat by name on every source. Use it first when
        {{username}} names someone or a chat. Other names {{username}} gave a person
        (Mom) work too. Returns: `person <ref>` lines with message count, last activity,
        accounts marked as the same person, other names, and what {{username}} wrote
        about the person; under each, the `chat <ref>` lines of the chats they write in,
        most messages first. Also `chat <ref>` lines of chats named so. A person
        reference goes in written_by; a chat reference goes in scope, or in chat of
        chat_archive__read_period. When a name matches several people, ask
        {{username}}."""
        from ..kb import identity
        from ..kb.retrieve.scope import person_card, chat_card, chat_candidates, person_candidates
        ids = await identity.snapshot(ctx.store)
        kbs = self._kbs()
        chat_kbs = kbs.chats()

        def run():
            people, chats = [], []
            for k in chat_kbs:
                exact, partial = person_candidates(k, name)
                people += [(0, p) for p in exact] + [(1, p) for p in partial]
                ce, cp = chat_candidates(k, name)
                chats += [(0, k.id, c) for c in ce] + [(1, k.id, c) for c in cp]
            # a same person's name or an alias: every account of that person, exact
            seen = {p["ref"] for _, p in people}
            named = [m for i in ids.named(name) for m in sorted(i.members) if m not in seen]
            for a in identity.accounts(kbs, named).values():
                given = [a["names"][k] for k in ("custom", "contact", "display", "push",
                                                 "username") if a["names"][k]]
                people.append((0, {**a, "names": given}))
            return people, chats
        people, chats = await anyio.to_thread.run_sync(run)
        people.sort(key=lambda x: (x[0], -x[1]["messages"]))
        lines: list[str] = []
        exact = [p for rank, p in people if rank == 0]
        if people:
            lines.append(f"people matching {name!r}"
                         + (f" ({len(exact)} exact)" if exact else "") + ":")
            told: set[int] = set()
            listed: set[str] = set()
            for rank, p in people[:15]:
                rec = ids.identity_of(p["ref"])
                line = "  " + person_card(p)
                if rec is not None and rec.same_person:
                    others = sorted(rec.members - {p["ref"]})
                    line += f", same person as {', '.join(others)} ('{rec.name}')"
                if rec is not None and rec.aliases:
                    line += f", also called {', '.join(rec.aliases)}"
                lines.append(line)
                if rec is not None and rec.about and rec.id not in told:
                    told.add(rec.id)
                    lines.append(f"    {self.me} wrote about them:")
                    lines += [f"      {t}" for t in rec.about.splitlines() if t.strip()]
                same = ids.expand(p["ref"])
                if (rank == 0 or len(people) == 1) and not same & listed:
                    listed |= same
                    chat_lines = await anyio.to_thread.run_sync(
                        lambda: _their_chats(kbs, same))
                    if chat_lines:
                        lines.append("    their chats, most messages first:")
                        lines += [f"      {c}" for c in chat_lines]
            # accounts marked as the same person are one person here
            per_kb: dict[str, set[str]] = {}
            for p in (exact or [p for _, p in people]):
                rec = ids.same_person(p["ref"])
                per_kb.setdefault(p["kb"], set()).add(f"person:{rec.id}" if rec else p["ref"])
            if any(len(n) > 1 for n in per_kb.values()):
                lines.append("  ambiguous: pass the references you mean in written_by, or "
                             "ask which one you mean.")
            first = (exact or [p for _, p in people])[0]["ref"]
            lines.append(f"  written_by=[\"{first}\"] searches what they wrote; a chat ref "
                         f"goes in scope, chat or read_period")
        if chats:
            chats.sort(key=lambda x: (x[0], -x[2]["messages"]))
            lines.append(f"chats matching {name!r}:")
            lines += ["  " + chat_card(k, c) for _, k, c in chats[:10]]
        if not lines:
            lines.append(f"No person and no chat matches {name!r}. Try part of the name, "
                         f"a nickname, or digits of the number.")
        return ToolResult(content="\n".join(lines),
                          display={"people": len(people), "chats": len(chats)})

    @tool(read_only=False, destructive=False)
    async def mark_same_person(
        self, ctx: ToolContext,
        accounts: Annotated[list[str], "Two or more account references from "
                                       "chat_archive__look_up."],
        name: Annotated[str, "The name for the person."],
    ) -> ToolResult:
        """Mark several accounts as one person. Do this only after {{username}} confirms it.
        Never do it because two names are equal. After this, `written_by`, counts and
        look-ups treat the accounts as one person."""
        from ..kb import identity
        store = ctx.store
        if store is None:
            return ToolResult(content="No database here.", is_error=True)
        if len(accounts) < 2:
            return ToolResult(content="Pass at least two account references. "
                                      "chat_archive__look_up lists them.", is_error=True)
        try:
            out = await store.write(lambda c: identity.link_sync(c, accounts, name=name,
                                                                 source="celine"))
        except identity.IdentityError as e:
            return ToolResult(content=str(e), is_error=True)
        return ToolResult(content=f"Marked as the same person, '{out['name']}': "
                                  f"{', '.join(out['members'])}",
                          display={"identity": out["id"]})

    @tool()
    async def read_session(
        self, ctx: ToolContext,
        ref: Annotated[str, "A message reference from a message line, such as "
                            "whatsapp:48213. Not a person or chat reference."],
        max_tokens: Annotated[int | None, "The most text you want back. A longer session "
                                          "is cut around the message."] = None,
    ) -> ToolResult:
        """Read the chat session that contains one message, in order. A chat session is
        the messages of one chat between two pauses of more than 90 minutes. Read it before
        you quote a line. Several references from one session return the same session.
        Returns: the messages with references, the message marked >, then `earlier` and
        `later` references."""
        return await self._read_ref(ctx, ref, max_tokens)

    @tool()
    async def read_period(
        self, ctx: ToolContext,
        chat: Annotated[str, "A chat reference from chat_archive__look_up or from a "
                             "result, such as whatsapp:213."],
        when: Annotated[str | None, "A date range: 2021-03, 2021-03-01..2021-03-14, \"last "
                                    "summer\", \"the last 3 months\". Default: the "
                                    "newest messages that fit."] = None,
        max_tokens: Annotated[int | None, "The most text you want back."] = None,
    ) -> ToolResult:
        """Read the messages of one chat in a date range, in order. Use it for summaries
        of a period. A summary cites a few of the messages it is based on, such as
        [[whatsapp:48213]]. A period too long for `max_tokens` returns one line per chat
        session. Read a line further with chat_archive__read_session."""
        if not REF.match(str(chat).strip()):
            return ToolResult(content=f"chat takes a chat reference, not a name: {chat!r}. "
                                      f"Look it up with chat_archive__look_up(name=...) and "
                                      f"pass its `chat <ref>` reference.", is_error=True)
        return await self._read_slice(ctx, chat, when, max_tokens)

    @tool()
    async def get_messages(
        self, ctx: ToolContext,
        refs: Annotated[list[str], "One or more message references, such as "
                                   "[\"whatsapp:48213\"]."],
    ) -> ToolResult:
        """Get messages by their references, with chat, sender and time. A reference never
        changes. Get a saved reference again before you cite it. Returns: the messages
        grouped by chat, then the references that match no message."""
        from ..kb.chat import ChatKB
        from ..kb.retrieve import episodes as E
        cap = int(self.cfg.r("chat_archive.get_max_refs"))
        items = [r.strip().strip("[]") for r in _as_list(refs)]
        items = list(dict.fromkeys(r for r in items if r))
        if not items:
            return ToolResult(content="Pass refs: message references like whatsapp:48213.",
                              is_error=True)
        extra = items[cap:]
        items = items[:cap]
        kbs = self._kbs()
        wanted: dict[str, list[int]] = {}
        bad: list[str] = []
        for r in items:
            kid, _, mid = r.partition(":")
            if isinstance(kbs.get(kid), ChatKB) and mid.isdigit():
                wanted.setdefault(kid, []).append(int(mid))
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
                found.add(f"{kid}:{x.id}")
            for cid, xs in by_chat.items():
                ch = info.get(cid) or {}
                chats.append({"ref": f"{kid}:{cid}", "name": ch.get("name") or "?",
                              "kind": ch.get("kind") or "chat", "platform": kid})
                blocks.append(_chat_head(kb.title, kid, cid, ch) + "\n" + "\n".join(
                    _line(kid, x) for x in xs))
        missing = [r for r in items if r not in found and r not in bad]
        tail = []
        if bad:
            tail.append(f"not message references (source:id): {', '.join(bad)}")
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
        ref: Annotated[str, "The message reference to scroll from."],
        direction: Annotated[Literal["before", "after", "both"], "before: older messages. "
                             "after: newer messages. both: each side."] = "both",
        count: Annotated[int, "Messages per side, 1 to 20."] = 20,
    ) -> ToolResult:
        """Get up to 20 messages before or after one message in its chat. To scroll
        further, call it from the first or last message it returned. Returns: the
        messages, oldest first, then the range they cover."""
        from ..kb.chat import ChatKB
        from ..kb.retrieve import episodes as E
        most = int(self.cfg.r("chat_archive.scroll_max_messages"))
        per_side = max(1, min(int(count or most), most))
        budget = self._cap(ctx, int(self.cfg.r("chat_archive.scroll_max_tokens")))
        kid, _, mid = str(ref or "").strip().strip("[]").partition(":")
        kb = self._kbs().get(kid)
        if not isinstance(kb, ChatKB) or not mid.isdigit():
            return ToolResult(content=f"{ref} is not a message reference (source:id), like "
                                      f"whatsapp:48213.", is_error=True)
        if direction not in ("before", "after", "both"):
            return ToolResult(content="direction is before, after or both.", is_error=True)
        sides = ["before", "after"] if direction == "both" else [direction]
        side_budget = budget // len(sides)

        def run(c):
            a = c.execute(f"{E.SELECT} WHERE m.id=?", (int(mid),)).fetchone()
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
            return ToolResult(content=f"There is no message {kid}:{mid}.", is_error=True)
        anchor, chat_id = got["anchor"], got["anchor"].chat_id
        ch = got["info"].get(chat_id) or {}
        before = got.get("before", ([], True))[0]
        after = got.get("after", ([], True))[0]
        seq = [*before, anchor, *after]

        body = []
        prev_day = None
        for x in seq:
            t = E.clock(x.ts)
            stamp = f"{t:%H:%M}" if t.date() == prev_day else f"{t:%Y-%m-%d %H:%M}"
            prev_day = t.date()
            mark = ">" if x.id == anchor.id else " "
            body.append(f"{mark} [{kid}:{x.id}] {stamp} {x.sender}: {x.text}")

        first, last = seq[0], seq[-1]
        a_t, b_t = E.clock(first.ts), E.clock(last.ts)
        covered = (f"these are {kid}:{first.id} ({a_t:%Y-%m-%d %H:%M}) to {kid}:{last.id} "
                   f"({b_t:%Y-%m-%d %H:%M}) of this chat")
        nxt = []
        if "before" in got:
            nxt.append("before: start of the chat reached" if got["before"][1] else
                       f"older: chat_archive__scroll(ref=\"{kid}:{first.id}\", "
                       f"direction=\"before\")")
        if "after" in got:
            nxt.append("after: newest message reached" if got["after"][1] else
                       f"newer: chat_archive__scroll(ref=\"{kid}:{last.id}\", "
                       f"direction=\"after\")")
        parts = []
        if "before" in got:
            parts.append(f"{len(before)} before")
        if "after" in got:
            parts.append(f"{len(after)} after")
        head = [self._chat_line(kid, {**ch, "id": chat_id}),
                _chat_head(kb.title, kid, chat_id, ch),
                f"{len(before) + len(after)} messages around {kid}:{anchor.id} "
                f"({', '.join(parts)}), oldest first:"]
        content = "\n".join(head + body + ["", covered + ".", "; ".join(nxt)])
        return ToolResult(content=content, display={
            "messages": len(before) + len(after), "anchor": f"{kid}:{anchor.id}",
            "direction": direction,
            "first": f"{kid}:{first.id}", "last": f"{kid}:{last.id}",
            "scope_kind": "chats", "scope_chats": [{
                "ref": f"{kid}:{chat_id}", "name": ch.get("name") or "?",
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
        earlier = f"{kb.id}:{e}" if e else None
        later = f"{kb.id}:{l_}" if l_ else None
        line = (f"earlier: {earlier or 'none (start of the chat)'}; "
                f"later: {later or 'none (newest message)'}")
        return line, {"earlier": earlier, "later": later}

    async def _wrong_ref(self, ctx, ref: str, want: str) -> str | None:
        """A person or chat reference passed where a `want` ("message" or "chat")
        reference belongs: what it is and what to pass instead. None when it is
        neither."""
        from ..kb import identity
        kbs = self._kbs()
        kid, _, n = ref.partition(":")
        kb = kbs.get(kid)
        if not hasattr(kb, "list_chats") or not n.isdigit():
            return None

        def run(c):
            person = c.execute("SELECT 1 FROM messages WHERE sender_id=? LIMIT 1",
                               (int(n),)).fetchone() is not None
            chat = _chat_info(c, {int(n)}).get(int(n)) if want != "chat" else None
            return person, chat
        person, chat = await anyio.to_thread.run_sync(lambda: kb.file.read_sync(run))
        parts = []
        if chat:
            parts.append(f"{ref} is a chat reference ({_chat_name(chat)}), not a {want} "
                         f"reference. Read a chat with chat_archive__read_period("
                         f"chat=\"{ref}\").")
        if person:
            ids = await identity.snapshot(ctx.store)
            chats = await anyio.to_thread.run_sync(lambda: _their_chats(kbs, ids.expand(ref)))
            parts.append(f"{ref} is a person reference, not a {want} reference. "
                         f"written_by=[\"{ref}\"] searches what they wrote. The chats they "
                         f"write in, most messages first:\n"
                         + "\n".join(f"  {c}" for c in chats))
        return "\n".join(parts) or None

    def _chat_line(self, kid: str, chat: dict) -> str:
        return (f"scope: 1 chat: {kid}:{chat.get('id')} \"{chat.get('name', '?')}\" "
                f"({chat.get('kind') or 'chat'})")

    async def _read_ref(self, ctx, ref: str, max_tokens: int | None) -> ToolResult:
        from ..kb.chat import ChatKB
        from ..kb.retrieve import episodes as E
        kid, _, mid = str(ref or "").strip().strip("[]").partition(":")
        kb = self._kbs().get(kid)
        if not isinstance(kb, ChatKB) or not mid.isdigit():
            return ToolResult(content=f"{ref} is not a message reference (source:id), like "
                                      f"whatsapp:48213.", is_error=True)
        m = await anyio.to_thread.run_sync(kb.message, int(mid))
        if m is None:
            wrong = await self._wrong_ref(ctx, f"{kid}:{mid}", "message")
            return ToolResult(content=wrong or f"There is no message {ref}.", is_error=True)
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
        head = [self._chat_line(kid, ep.chat or chat)]
        if total > len(msgs):
            head.append(f"its chat session has {total:,} messages. {len(msgs):,} around it "
                        f"shown.")
        else:
            head.append(f"its whole chat session, {total:,} messages")
        body = "\n".join(head) + "\n" + E.render(ep, kb_title=kb.title) + "\n" + handles
        return ToolResult(content=body, display={"chat": chat.get("name"),
                                                 "messages": len(msgs), "scope_kind": "chats",
                                                 "scope_chats": [{"ref": f"{kid}:{chat_id}",
                                                                  "name": (ep.chat or chat).get("name"),
                                                                  "kind": (ep.chat or chat).get("kind"),
                                                                  "platform": kid}],
                                                 **hd})

    def _session_around(self, kb, chat_id: int, m: dict, budget: int):
        """The chat session the message belongs to, trimmed around it to the budget: from
        the semantic topic search index's sessions when the message is in one, else cut
        here by the same 90-minute pause rule. -> (messages shown, session size)."""
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
                gap = S.SessionParams().gap_ms
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
        from ..kb.retrieve.scope import Ambiguous, resolve_chat
        from ..kb.retrieve.when import parse_when
        cap = int(self.cfg.r("chat_archive.max_tokens_cap"))
        budget = min(int(max_tokens or self.cfg.r("chat_archive.period_max_tokens")), cap)
        budget = self._cap(ctx, budget)
        kbs = self._kbs()
        chat_kbs = kbs.chats()
        try:
            found, label = await anyio.to_thread.run_sync(lambda: resolve_chat(chat_kbs, chat))
            rng = parse_when(dates_for(when))
        except Ambiguous as e:
            return ToolResult(content="\n".join(e.lines), display={"ambiguous": True})
        except ValueError as e:
            return ToolResult(content=str(e), is_error=True)
        (kid, ids), = found.items()
        cid = next(iter(ids))
        kb = kbs.get(kid)
        info = await anyio.to_thread.run_sync(
            lambda: kb.file.read_sync(lambda c: _chat_info(c, {cid}))) if kb else {}
        if cid not in info:
            # a person or message reference looks the same: "no messages" would be false
            wrong = await self._wrong_ref(ctx, f"{kid}:{cid}", "chat")
            return ToolResult(content=wrong or (
                f"{kid}:{cid} is not a chat. chat takes a chat reference, the `chat ...` "
                f"lines of chat_archive__look_up. Message and account references look the "
                f"same but are not chats."), is_error=True)
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
        scope_line = self._chat_line(kid, chat_row)
        disp = {"chat": f"{kid}:{cid}", "scope_kind": "chats",
                "scope_chats": [{"ref": f"{kid}:{cid}", "name": chat_row.get("name"),
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
        """A long range as one line per chat session (cut at 90-minute pauses): start,
        size, who, and its longest message with its reference. Only an index to read
        further: quotes come from read_session, never from here."""
        from ..kb import sessions as S
        from ..kb.retrieve import episodes as E
        rows = conn.execute(f"{E.SELECT} WHERE {where} ORDER BY m.ts, m.id LIMIT 60000",
                            args).fetchall()
        msgs = [E.to_msg(r, self.me) for r in rows]
        parts = S.cut([(i, x.ts, len(x.text)) for i, x in enumerate(msgs)],
                      S.SessionParams().gap_ms)
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
            text = " ".join(longest.text.split())[:90]
            line = (f"  {span}; {len(part)} msgs; {top}; [{kid}:"
                    f"{longest.id}] {longest.sender}: {text}")
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
