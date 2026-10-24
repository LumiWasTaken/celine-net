"""Scope: which knowledge bases, chats, people and time range a retrieval covers.

People and chats are resolved on the server, deterministically, and **ambiguity is
returned, not guessed**: "Alex" with two exact matches in one knowledge base stops the
call and lists the candidates with their references.

`people` means messages they wrote; "me" (or "ich", or the configured user name) means
messages you wrote.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import names
from ..identity import REF, Identities, norm_name
from .when import parse_when

ME_WORDS = {"me", "i", "ich", "myself", "mich", "mir", "you", "user"}


@dataclass
class Scope:
    kbs: list[str]
    chats: dict[str, set[int]] | None = None        # None: every chat
    senders: dict[str, set[int]] | None = None      # None: anyone
    me: bool = False
    ts_from: int | None = None
    ts_to: int | None = None
    hours: set[int] | None = None                   # local hours of day; None: any (count)
    labels: list[str] = field(default_factory=list)  # how the scope was resolved

    @property
    def filters_people(self) -> bool:
        return self.senders is not None or self.me

    def kb_in_scope(self, kid: str) -> bool:
        if kid not in self.kbs:
            return False
        if self.chats is not None and not self.chats.get(kid):
            return False
        if self.filters_people and not self.me and not self.senders.get(kid):
            return False
        return True

    def sql(self, kid: str, alias: str = "m") -> tuple[list[str], list[Any]]:
        """WHERE clauses (ANDed) and their args for messages `alias` of one kb."""
        where: list[str] = []
        args: list[Any] = []
        if self.chats is not None:
            ids = sorted(self.chats.get(kid, ()))
            where.append(f"{alias}.chat_id IN ({','.join('?' * len(ids)) or 'NULL'})")
            args += ids
        if self.filters_people:
            ors = []
            s = sorted((self.senders or {}).get(kid, ()))
            if s:
                ors.append(f"{alias}.sender_id IN ({','.join('?' * len(s))})")
                args += s
            if self.me:
                ors.append(f"{alias}.from_me = 1")
            where.append("(" + (" OR ".join(ors) or "0") + ")")
        if self.ts_from is not None:
            where.append(f"{alias}.ts >= ?")
            args.append(self.ts_from)
        if self.ts_to is not None:
            where.append(f"{alias}.ts < ?")
            args.append(self.ts_to)
        if self.hours is not None:
            hs = sorted(self.hours)
            where.append(f"CAST(strftime('%H', {alias}.ts / 1000, 'unixepoch', 'localtime') "
                         f"AS INTEGER) IN ({','.join('?' * len(hs)) or 'NULL'})")
            args += hs
        return where, args


class Ambiguous(Exception):
    """Raised with the lines the model needs to pick: every candidate with its ref."""

    def __init__(self, lines: list[str]):
        super().__init__("\n".join(lines))
        self.lines = lines


# ---- people -------------------------------------------------------------------------

def person_candidates(kb, name: str, limit: int = 12) -> tuple[list[dict], list[dict]]:
    """(exact, partial) matches in one chat kb, by messages sent, most first."""
    n = norm_name(name)
    digits = "".join(c for c in name if c.isdigit())

    def run(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        like = f"%{name.strip()}%"
        cond = ("p.custom_name LIKE ? OR p.contact_name LIKE ? OR p.push_name LIKE ? "
                "OR p.display_name LIKE ? OR p.username LIKE ?")
        args: list[Any] = [like] * 5
        if len(digits) >= 5:
            cond += " OR p.phone LIKE ?"
            args.append(f"%{digits}%")
        return conn.execute(
            f"""SELECT p.*, (SELECT COUNT(*) FROM messages WHERE sender_id = p.id) AS n,
                       (SELECT MAX(ts) FROM messages WHERE sender_id = p.id) AS last_ts
                FROM people p WHERE p.is_me = 0 AND ({cond})
                ORDER BY n DESC LIMIT 200""", args).fetchall()
    rows = kb.file.read_sync(run)
    exact, partial = [], []
    for r in rows:
        given = [r[c] for c in names.ORDER if r[c] and str(r[c]).strip()]
        display, _ = names.resolve(r)
        p = {"ref": f"{kb.id}:{r['id']}", "kb": kb.id, "id": r["id"], "name": display,
             "names": given, "messages": r["n"], "last_ts": r["last_ts"],
             "phone": names.format_phone(r["phone"])}
        if n and any(norm_name(g) == n for g in given):
            exact.append(p)
        elif len(digits) >= 5 and digits in "".join(c for c in (r["phone"] or "") if c.isdigit()):
            exact.append(p)
        else:
            partial.append(p)
    return exact[:limit], partial[:limit]


def person_card(p: dict) -> str:
    last = datetime.fromtimestamp(p["last_ts"] / 1000).strftime("%Y-%m") if p.get("last_ts") else "-"
    other = [g for g in p.get("names", []) if g != p["name"]][:2]
    return (f"person {p['ref']} {p['name']}" + (f" (also {', '.join(other)})" if other else "")
            + (f", {p['phone']}" if p.get("phone") and p["phone"] != p["name"] else "")
            + f", {p['messages']:,} message{'' if p['messages'] == 1 else 's'}, last {last}")


def resolve_person(kbs: list, name: str, ids: Identities) -> tuple[set[str], str]:
    """-> (person refs, label). Raises Ambiguous or ValueError (nobody)."""
    name = str(name).strip()
    m = REF.match(name)
    if m:
        return ids.expand(name), name
    # a same person's name or an alias the user gave: exact, before any account name
    named = ids.named(name)
    if len(named) == 1:
        i = named[0]
        return set(i.members), (f"{name} → " + (f"{i.name}: " if i.name else "")
                                + ", ".join(sorted(i.members)))
    if len(named) > 1:
        raise Ambiguous([f"{name!r} names {len(named)} people. Pass the one you mean as "
                         f"account references (written_by=[\"source:id\"]):"]
                        + [f"  {i.name or 'person'}: {', '.join(sorted(i.members))}"
                           for i in named])
    found: set[str] = set()
    ambiguous: list[dict] = []
    labels = []
    for kb in kbs:
        exact, partial = person_candidates(kb, name)
        pick = exact or partial
        # several rows that are one linked identity are one person
        groups: dict[str, list[dict]] = {}
        for p in pick:
            ident = ids.identity_of(p["ref"])
            groups.setdefault(f"i{ident.id}" if ident else p["ref"], []).append(p)
        if len(groups) == 1:
            ps = next(iter(groups.values()))
            for p in ps:
                found |= ids.expand(p["ref"])
            labels.append(", ".join(f"{p['ref']} {p['name']}" for p in ps))
        elif len(groups) > 1:
            ambiguous.extend(pick)
    if ambiguous:
        ambiguous.sort(key=lambda p: -p["messages"])
        lines = [f"{name!r} matches {len(ambiguous)} people. Pass the one you mean as an "
                 f"account reference (written_by=[\"source:id\"]), or several:"]
        lines += ["  " + person_card(p) for p in ambiguous[:10]]
        if len(ambiguous) > 10:
            lines.append(f"  and {len(ambiguous) - 10} more. Use chat_archive__look_up with a "
                         f"longer name.")
        raise Ambiguous(lines)
    if not found:
        found, label = _typo_fallback(kbs, name, ids)
        labels.append(label)
    if not found:
        raise ValueError(f"Nobody called {name!r} is in the Messages archive. Try "
                         f"chat_archive__look_up with part of the name or digits of the "
                         f"phone number.")
    unlinked = len({r.split(':')[0] for r in found}) > 1 and not any(
        ids.same_person(r) for r in found)
    return found, (f"{name} → {'; '.join(labels)}"
                   + (" (not marked as the same person, see chat_archive__look_up)"
                      if unlinked else ""))


def _typo_fallback(kbs: list, name: str, ids: Identities) -> tuple[set[str], str]:
    """One-typo matches, as instant search finds them (kb/query.py). One candidate in
    total is taken; more are listed for the model to pick."""
    from ..query import resolve_people
    cands = resolve_people(kbs, name).candidates
    if not cands:
        return set(), ""
    if len(cands) > 1:
        lines = [f"{name!r} is not a name in the Messages archive. Close ones (pass the one "
                 f"you mean as a reference):"]
        lines += [f"  person {c['ref']} {c['name']}, {c['messages']:,} messages"
                  for c in cands[:10]]
        raise Ambiguous(lines)
    ref = cands[0]["ref"]
    return ids.expand(ref), f"{ref} {cands[0]['name']} (closest spelling)"


# ---- chats --------------------------------------------------------------------------

def chat_candidates(kb, name: str) -> tuple[list[dict], list[dict]]:
    res = kb.list_chats(name, "count", limit=50)["chats"]
    n = norm_name(name)
    exact = [c for c in res if norm_name(c["name"]) == n]
    return exact, [c for c in res if c not in exact]


def chat_card(kid: str, c: dict) -> str:
    span = ""
    if c.get("first_ts"):
        span = (f", {datetime.fromtimestamp(c['first_ts'] / 1000):%Y-%m} to "
                f"{datetime.fromtimestamp(c['last_ts'] / 1000):%Y-%m}")
    return (f"chat {kid}:{c['id']} {c['name']} ({c['kind']}"
            + (f", {c['parent']}" if c.get("parent") else "")
            + f") {c['messages']:,} message{'' if c['messages'] == 1 else 's'}{span}")


def resolve_chat(kbs: list, name: str) -> tuple[dict[str, set[int]], str]:
    name = str(name).strip()
    m = REF.match(name)
    if m:
        return {m[1]: {int(m[2])}}, name
    exact_all: list[tuple[str, dict]] = []
    partial_all: list[tuple[str, dict]] = []
    for kb in kbs:
        exact, partial = chat_candidates(kb, name)
        exact_all += [(kb.id, c) for c in exact]
        partial_all += [(kb.id, c) for c in partial]
    pick = exact_all or partial_all
    if not pick:
        raise ValueError(f"No chat called {name!r}. scope takes [\"all\"], source ids, or "
                         f"chat references from chat_archive__look_up (its `chat ...` "
                         f"lines).")
    if len(pick) > 1:
        pick.sort(key=lambda kc: -kc[1]["messages"])
        lines = [f"{name!r} matches {len(pick)} chats. Pass the ones you mean as their "
                 f"references in scope:"]
        lines += ["  " + chat_card(k, c) for k, c in pick[:10]]
        raise Ambiguous(lines)
    kid, c = pick[0]
    return {kid: {c["id"]}}, f"{name} → chat {kid}:{c['id']} {c['name']}"


# ---- all of it ----------------------------------------------------------------------

def resolve(kbs, *, kb_ids: str | list[str] | None = None,
            people: list[str] | None = None, chats: list[str] | None = None,
            when: str | None = None, identities: Identities | None = None,
            me_names: set[str] | None = None, now: datetime | None = None) -> Scope:
    """Everything the model passed, resolved. Raises Ambiguous (candidates) or
    ValueError (a sentence saying what could not be resolved)."""
    from ..chat import ChatKB
    ids = identities or Identities()
    chat_kbs = [k for k in kbs.pick(kb_ids) if isinstance(k, ChatKB)]
    if not chat_kbs:
        raise ValueError(f"no chat source called {kb_ids!r}")
    scope = Scope(kbs=[k.id for k in chat_kbs])
    me_words = ME_WORDS | {w.casefold() for w in (me_names or set())}

    rng = parse_when(when, now)
    if rng:
        scope.ts_from, scope.ts_to, label = rng
        scope.labels.append(f"when: {label}")

    problems: list[str] = []
    if chats:
        scope.chats = {}
        for name in chats:
            try:
                found, label = resolve_chat(chat_kbs, name)
            except Ambiguous as e:
                problems += e.lines
                continue
            for k, s in found.items():
                scope.chats.setdefault(k, set()).update(s)
            scope.labels.append(f"chat: {label}")

    if people:
        scope.senders = {}
        for name in people:
            if str(name).strip().casefold() in me_words:
                scope.me = True
                scope.labels.append(f"person: {name} → you")
                continue
            try:
                refs, label = resolve_person(chat_kbs, name, ids)
            except Ambiguous as e:
                problems += e.lines
                continue
            for r in refs:
                k, _, pid = r.partition(":")
                scope.senders.setdefault(k, set()).add(int(pid))
            scope.labels.append(f"person: {label}")
    if problems:
        raise Ambiguous(problems)
    return scope
