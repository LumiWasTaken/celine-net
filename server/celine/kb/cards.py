"""A person as the agent reads them: everything known, in one block.

    person discord:person:71 Moon (username shiniloli), 79 messages, last 2026-11-20
      pronouns: she/her
      bio: sleepy
      earlier: display name Luna until 2026-03-01
      nickname on Chess server: Moony
      relation to lumi: sister
      also called Sis
      notes:
        2026-11-20, lumi: birthday on 3 May
        2026-11-22, you: moved to Hamburg
      the direct chat with them: chat discord:chat:18 Moon (dm with discord:person:71) ...
      other chats they write in, most messages first:
        chat discord:chat:40 Chess server #general (channel), 12 by them, last 2026-10-02

`chat_archive__look_up` returns it, and a turn gets it on its own when the user's message
names one person (skills/people.py). Sync: it reads the sources.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from . import identity
from .identity import Identities, chat_ref, parse_person
from .retrieve.scope import chat_card, direct_chats, person_card

_KIND = {"username": "username", "display_name": "display name", "push_name": "WhatsApp name",
         "contact_name": "contact name", "bio": "bio", "pronouns": "pronouns"}


def _day(ts: int | None) -> str:
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else "?"


def written_in(kbs, accounts, limit: int = 8) -> list[tuple[str, str]]:
    """(chat reference, its line) for the chats these accounts (one person) write in,
    most messages first, with how many they wrote there and when last."""
    from .chat import chat_cards
    merged: dict[str, dict] = {}            # two accounts of one person in one chat
    for ref in sorted(accounts):
        kid, pid = parse_person(ref)
        kb = kbs.get(kid)
        if not hasattr(kb, "list_chats"):
            continue

        def run(c, pid=pid):
            rows = c.execute("SELECT chat_id, COUNT(*), MAX(ts) FROM messages WHERE "
                             "sender_id=? GROUP BY chat_id ORDER BY COUNT(*) DESC LIMIT ?",
                             (pid, limit)).fetchall()
            info = chat_cards(c, {r[0] for r in rows})
            return [{**info.get(r[0], {}), "id": r[0], "n": r[1], "last_ts": r[2]}
                    for r in rows]
        for ch in kb.file.read_sync(run):
            m = merged.setdefault(chat_ref(kid, ch["id"]), {**ch, "n": 0, "last_ts": 0})
            m["n"] += ch["n"]
            m["last_ts"] = max(m["last_ts"], ch["last_ts"] or 0)
    top = sorted(merged.items(), key=lambda kv: -kv[1]["n"])[:limit]

    def name(c: dict) -> str:
        return f"{c['parent']} #{c.get('name') or '?'}" if c.get("parent") else c.get("name") or "?"
    return [(ref, f"chat {ref} {name(ch)} ({ch.get('kind') or 'chat'}), {ch['n']:,} by "
                  f"them, last {_day(ch['last_ts'])}") for ref, ch in top]


def _history(rows: list[dict[str, Any]]) -> list[str]:
    """What an account was called before and its nicknames, from its name history."""
    out = []
    was = [f"{_KIND[r['kind']]} {r['value']} until {_day(r['to_ts'])}" for r in rows
           if r["to_ts"] is not None and r["kind"] in _KIND and r["kind"] not in ("bio",)]
    if was:
        out.append("earlier: " + "; ".join(was))
    for r in rows:
        if r["kind"] == "nick" and r["to_ts"] is None:
            out.append(f"nickname{' on ' + r['scope'] if r['scope'] else ''}: {r['value']}")
    return out


def person(kbs, ids: Identities, ref: str, me: str, *, chats: bool = True) -> list[str]:
    """The card of the person account `ref` belongs to: every account of theirs, then what
    is kept about the person, then their chats. [] when there is no such account."""
    members = ids.expand(ref)
    accts = identity.accounts(kbs, members)
    group = sorted(accts.values(), key=lambda a: -(a["messages"] or 0))
    if not group:
        return []
    history: dict[str, list[dict]] = {}
    by_kb: dict[str, list[int]] = {}
    for a in group:
        by_kb.setdefault(a["kb"], []).append(a["id"])
    for kid, pids in by_kb.items():
        for pid, rows in kbs.get(kid).name_history(pids).items():
            history[identity.person_ref(kid, pid)] = rows
    lines: list[str] = []
    for a in group:
        lines.append(person_card(a))
        if a.get("deleted"):
            lines.append("  this account no longer exists; these are its last names")
        if a.get("pronouns"):
            lines.append(f"  pronouns: {a['pronouns']}")
        if a.get("bio"):
            bio = a["bio"].strip().splitlines()
            lines.append(f"  bio: {bio[0]}")
            lines += [f"    {ln}" for ln in bio[1:]]
        lines += [f"  {h}" for h in _history(history.get(a["ref"], []))]
    rec = ids.identity_of(group[0]["ref"])
    if rec is not None and rec.same_person:
        lines.append(f"  these accounts are one person, '{rec.name}'")
    if rec is not None and rec.relation:
        lines.append(f"  relation to {me}: {rec.relation}")
    if rec is not None and rec.aliases:
        lines.append(f"  also called {', '.join(rec.aliases)}")
    if rec is not None and rec.notes:
        lines.append("  notes:")
        for n in rec.notes:
            who = me if n["author"] == "user" else "you"
            text = n["text"].strip().splitlines()
            lines.append(f"    {n['created_at'][:10]}, {who}: {text[0]}")
            lines += [f"      {ln}" for ln in text[1:]]
    if chats:
        refs = {a["ref"] for a in group}
        chat_kbs = list(kbs.chats())
        direct = direct_chats(chat_kbs, refs)
        shown = {chat_ref(k, c["id"]) for k, c in direct}
        for k, c in direct:
            lines.append("  the direct chat with them: " + chat_card(k, c))
        other = [line for r, line in written_in(kbs, refs) if r not in shown]
        if other:
            lines.append("  other chats they write in, most messages first:")
            lines += [f"    {c}" for c in other]
    return lines
