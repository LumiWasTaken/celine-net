"""Scope: which knowledge bases, chats, people and time range a retrieval covers.

People and chats are passed as references (kb/identity.py). A name is refused with the
people or chats it could mean: nothing is guessed.

`people` means messages they wrote; "me" (or "ich", or the configured user name) means
messages you wrote.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import names
from ..identity import (CHAT_REF, PERSON_REF, Identities, chat_ref, kind_of, not_a,
                        parse_person, person_ref)
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


# ---- people and chats ---------------------------------------------------------------
# A parameter takes references only. A name passed instead is refused with who or what it
# could mean (kb/query.py, the one name resolver), so the caller passes the reference.

def labelled_names(names_: dict[str, str | None], shown: str) -> list[str]:
    """Every name of an account (identity.accounts: `names`) but the one shown, each
    with what it is: `username shiniloli`, `display name Moon`."""
    return names.labelled({f"{k}_name" if k != "username" else k: v
                           for k, v in names_.items()}, shown)


def person_card(a: dict) -> str:
    """One account as a line: its reference, the name shown, its other names with
    their labels, how much it wrote and when last."""
    last = datetime.fromtimestamp(a["last_ts"] / 1000).strftime("%Y-%m") if a.get("last_ts") else "-"
    other = labelled_names(a.get("names") or {}, a["name"])
    n = a.get("messages") or 0
    return (f"person {a['ref']} {a['name']}" + (f" ({'; '.join(other)})" if other else "")
            + (f", {a['phone']}" if a.get("phone") and a["phone"] != a["name"] else "")
            + f", {n:,} message{'' if n == 1 else 's'}, last {last}")


def chat_card(kid: str, c: dict) -> str:
    span = ""
    if c.get("first_ts"):
        span = (f", {datetime.fromtimestamp(c['first_ts'] / 1000):%Y-%m-%d} to "
                f"{datetime.fromtimestamp(c['last_ts'] / 1000):%Y-%m-%d}")
    kind = c["kind"]
    if kind == "dm" and c.get("person"):
        kind = f"dm with {person_ref(kid, c['person'])}"
    return (f"chat {chat_ref(kid, c['id'])} {c['name']} ({kind}"
            + (f", {c['parent']}" if c.get("parent") else "")
            + f") {c['messages']:,} message{'' if c['messages'] == 1 else 's'}{span}")


def direct_chats(kbs: list, accounts) -> list[tuple[str, dict]]:
    """The direct chats with these accounts (the chat's peer), most messages first:
    also the ones where only you wrote."""
    out: list[tuple[str, dict]] = []
    by_kb: dict[str, list[int]] = {}
    for ref in accounts:
        m = PERSON_REF.match(ref)
        if m:
            by_kb.setdefault(m[1], []).append(int(m[2]))
    for kb in kbs:
        if kb.id in by_kb:
            out += [(kb.id, c) for c in kb.chats_with(by_kb[kb.id])]
    return sorted(out, key=lambda kc: -kc[1]["messages"])


def could_be(kbs: list, name: str, what: str, ids: Identities) -> list[str]:
    """The people or chats a name could mean, as card lines."""
    from ..identity import accounts
    from ..query import resolve_chats, resolve_people
    if what == "person":
        res = resolve_people(kbs, name, identities=ids)
        found = accounts(kbs, res.refs[:10])
        return ["  " + person_card(found[r]) for r in res.refs[:10] if r in found]
    res = resolve_chats(kbs, name)
    rows = {(k.id, c["id"]): c for k in kbs for c in k.chats_by_id(
        [c["id"] for c in res.candidates if c["kb"] == k.id])}
    return ["  " + chat_card(c["kb"], rows[(c["kb"], c["id"])]) for c in res.candidates[:10]
            if (c["kb"], c["id"]) in rows]


def refused(kbs: list, value: str, want: str, param: str, ids: Identities) -> str:
    """What to say for `value` passed in `param`, which takes `want` references."""
    kind = kind_of(value)
    if kind == "person" and want == "chat":
        chats = direct_chats(kbs, ids.expand(value))
        return (f"{not_a(value, want)} written_by=[\"{value}\"] is what they wrote."
                + ("\nThe direct chat with them:\n" + "\n".join(
                    "  " + chat_card(k, c) for k, c in chats) if chats else ""))
    if kind == "chat" and want == "person":
        return f"{not_a(value, want)} chats=[\"{value}\"] searches inside that chat."
    if kind == want or kind == "message" and want == "message":
        return f"There is no {want} {value}."
    if kind:
        return not_a(value, want)
    lines = could_be(kbs, value, want, ids)
    head = f"{param} takes {want} references, not names."
    if not lines:
        return (f"{head} No {want} is called {value!r}. chat_archive__look_up(name=...) "
                f"finds people and chats by part of a name.")
    return f"{head} {value!r} could be:\n" + "\n".join(lines)


def resolve_person(kbs: list, value: str, ids: Identities,
                   param: str = "written_by") -> tuple[set[str], str]:
    """A person reference -> (every account of that person, the label for the result).
    Raises ValueError with what to pass instead."""
    value = str(value).strip()
    m = PERSON_REF.match(value)
    kb = next((k for k in kbs if m and k.id == m[1]), None)
    if kb is None:
        raise ValueError(refused(kbs, value, "person", param, ids))
    name = kb.person_name(int(m[2]))
    if name is None:
        raise ValueError(f"There is no person {value}.")
    return ids.expand(value), f"{value} {name}"


def resolve_chat(kbs: list, value: str, ids: Identities | None = None,
                 param: str = "chats") -> tuple[str, int]:
    """A chat reference -> (source id, chat id). Raises ValueError with what to pass
    instead."""
    value = str(value).strip()
    m = CHAT_REF.match(value)
    kb = next((k for k in kbs if m and k.id == m[1]), None)
    if kb is None:
        raise ValueError(refused(kbs, value, "chat", param, ids or Identities()))
    if not kb.chats_by_id([int(m[2])]):
        raise ValueError(f"There is no chat {value}.")
    return m[1], int(m[2])


# ---- all of it ----------------------------------------------------------------------

def resolve(kbs, *, kb_ids: str | list[str] | None = None,
            people: list[str] | None = None, chats: list[str] | None = None,
            when: str | None = None, identities: Identities | None = None,
            me_names: set[str] | None = None, now: datetime | None = None) -> Scope:
    """Everything the model passed, resolved. Raises ValueError with a sentence saying
    what could not be resolved and what to pass instead."""
    from ..chat import ChatKB
    ids = identities or Identities()
    chat_kbs = [k for k in kbs.pick(kb_ids) if isinstance(k, ChatKB)]
    if not chat_kbs:
        raise ValueError(f"no chat source called {kb_ids!r}")
    scope = Scope(kbs=[k.id for k in chat_kbs])
    every = [k for k in kbs.pick(None) if isinstance(k, ChatKB)]   # a reference names its source
    me_words = ME_WORDS | set(me_names or ())

    rng = parse_when(when, now)
    if rng:
        scope.ts_from, scope.ts_to, label = rng
        scope.labels.append(f"when: {label}")

    if chats:
        scope.chats = {}
        for value in chats:
            kid, cid = resolve_chat(every, value, ids)
            scope.chats.setdefault(kid, set()).add(cid)
            scope.labels.append(f"chat: {value}")

    if people:
        scope.senders = {}
        for value in people:
            if value in me_words:
                scope.me = True
                scope.labels.append(f"person: {value} → you")
                continue
            refs, label = resolve_person(every, value, ids)
            for r in refs:
                k, pid = parse_person(r)
                scope.senders.setdefault(k, set()).add(pid)
            scope.labels.append(f"person: {label}")
    return scope
