"""Check a genre file of the labelled set (genres/<genre>.json): shape, counts, the
header layout, weekdays, and that a username is only in a header.

    python3 check_set.py genres/gaming.json [...]
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime

KINDS = ("literal", "paraphrase", "deep", "xling", "name")
DAY = re.compile(r"^(\w+) (\d{1,2}) (\w+) (20\d\d), (morning|afternoon|evening|night)$")
CHAT = re.compile(r'^(WhatsApp chat with .+|Discord chat with .+ \(username \S+\)|'
                  r'WhatsApp group ".+"|Discord server ".+" #\S+)$')


def check(path: str) -> list[str]:
    bad: list[str] = []
    d = json.load(open(path, encoding="utf-8"))
    ps, ans, non = d["passages"], d["answered"], d["unanswered"]
    if len(ps) != 20:
        bad.append(f"{len(ps)} passages, 20 wanted")
    handles: set[str] = set()
    cards = d["genre"].startswith("cards_")       # real character cards: line 1 names card and author
    for i, p in enumerate(ps):
        lines = p.split("\n")
        if cards:
            m = re.match(r"Character card: .+ \(by (.+)\)$", lines[0])
            if not m:
                bad.append(f"passage {i}: line 1 is no card header: {lines[0]!r}")
            else:
                handles.add(m[1])
            continue
        if len(lines) < 6 or len(lines) > 10:
            bad.append(f"passage {i}: {len(lines) - 3} message lines, 3 to 7 wanted")
            continue
        if not CHAT.match(lines[0]):
            bad.append(f"passage {i}: line 1 is no chat header: {lines[0]!r}")
        m = DAY.match(lines[2])
        if not m:
            bad.append(f"passage {i}: line 3 is no day: {lines[2]!r}")
        else:
            try:
                real = datetime.strptime(f"{m[2]} {m[3]} {m[4]}", "%d %B %Y").strftime("%A")
                if real != m[1]:
                    bad.append(f"passage {i}: {m[2]} {m[3]} {m[4]} is a {real}, not {m[1]}")
            except ValueError:
                bad.append(f"passage {i}: no such date: {lines[2]!r}")
        here = set(re.findall(r"\(username (\S+?)\)", lines[0] + "\n" + lines[1]))
        handles |= here
        people = [re.sub(r" \(username \S+\)", "", x).strip() for x in lines[1].split(",")]
        for ln in lines[3:]:
            who = ln.split(":", 1)[0]
            if ":" not in ln or who not in people:
                bad.append(f"passage {i}: {who!r} writes and is not on line 2")
        body = "\n".join(lines[3:]).casefold()
        for h in here:
            if h.casefold() in body:
                bad.append(f"passage {i}: the username {h} is in a message line")
    kinds = Counter(a["kind"] for a in ans)
    if len(ans) != 30 or any(kinds[k] != 6 for k in KINDS):
        bad.append(f"answered: {dict(kinds)}, 6 of each of {KINDS} wanted")
    for j, a in enumerate(ans):
        if not isinstance(a["passage"], int) or not 0 <= a["passage"] < len(ps):
            bad.append(f"answered {j}: passage {a['passage']!r} is no index")
        elif a["kind"] == "name" and not any(
                h.casefold() in a["q"].casefold() and h in ps[a["passage"]] for h in handles):
            bad.append(f"answered {j}: a name description names no username of its passage")
    kinds = Counter(u["kind"] for u in non)
    if kinds != {"hard": 12, "far": 8}:
        bad.append(f"unanswered: {dict(kinds)}, 12 hard and 8 far wanted")
    qs = [x["q"] for x in ans + non]
    bad += [f"the description {q!r} is there twice" for q, n in Counter(qs).items() if n > 1]
    return bad


if __name__ == "__main__":
    failed = False
    for path in sys.argv[1:]:
        problems = check(path)
        failed |= bool(problems)
        print(path, "ok" if not problems else "")
        for p in problems:
            print("  ", p)
    sys.exit(1 if failed else 0)
