"""Voice samples: replies of its own the user marked "Love this", kept in its notebook note
samples.md so the voice is there in every conversation (how it talks otherwise is its
talk, her.py). The note is loaded
always (its `load: always` line); the newest KEEP samples sit under its head, so the
note costs little, older ones fold into its `## earlier` section, which the loader
leaves out (turns/loads.py) and which keeps every one. Which
message is a sample is in kv `sample:<message id>`, its value the exact entry, so the
heart shows filled and Delete finds the entry again."""
from __future__ import annotations

PATH = "samples.md"
KEY = "sample:"
KEEP = 8
EARLIER = "## earlier"           # turns/loads.py carries a note only up to this heading
HEAD = ("load: always\n"
        "How I talk: replies {{username}} marked. Match this voice.\n")


def entry(text: str, day: str, model: str | None) -> str:
    """One sample: a heading with the date and the model, then its text as it wrote it."""
    return f"### {day}{f', {model}' if model else ''}\n{text.strip()}\n"


def _split(body: str) -> tuple[str, list[str], list[str]]:
    """(the head, the newest entries, the earlier ones) of the note's text, the stamp
    line already gone."""
    # note: an entry is cut at a line starting "### "; a sample with such a heading
    # of its own splits in two (Delete then leaves its tail)
    top, _, earlier = body.partition(f"\n{EARLIER}\n")
    head, *recent = top.split("\n### ")
    old = ("\n" + earlier).split("\n### ")[1:]
    return (head.rstrip("\n") + "\n", [f"### {e.strip()}\n" for e in recent],
            [f"### {e.strip()}\n" for e in old])


def _join(head: str, recent: list[str], earlier: list[str]) -> str:
    out = head + "".join(f"\n{e}" for e in recent)
    if earlier:
        out += f"\n{EARLIER}\n" + "".join(f"\n{e}" for e in earlier)
    return out


def added(body: str | None, new: str) -> str:
    """The note with `new` first; past KEEP the oldest move to the top of earlier."""
    head, recent, earlier = _split(body) if body and body.strip() else (HEAD, [], [])
    recent = [new, *recent]
    return _join(head, recent[:KEEP], recent[KEEP:] + earlier)


def removed(body: str, old: str) -> str:
    """The note without the entry `old` (wherever it is); unchanged when it is gone."""
    head, recent, earlier = _split(body)
    want = old.strip()
    return _join(head, [e for e in recent if e.strip() != want], [e for e in earlier if e.strip() != want])


async def mark(store, cfg, cid: str, mid: str, on: bool) -> bool:
    """The agent's reply `mid` in or out of the note: the kv row is claimed first, in one write,
    so two quick clicks change the note once. -> on."""
    from datetime import datetime, timezone

    from ..clock import zone
    from ..errors import CelineError, NotFound
    from ..skills.notebook import write_note
    row = await store.read(lambda c: c.execute(
        "SELECT m.role, m.content, t.model FROM messages m "
        "LEFT JOIN turns t ON t.id = m.turn_id "
        "WHERE m.id=? AND m.conversation_id=?", (mid, cid)).fetchone())
    if row is None:
        raise NotFound("message")
    if row["role"] != "assistant" or not (row["content"] or "").strip():
        raise CelineError("bad_request", "Only {{name}}'s replies can be samples.", 400)
    if on:
        day = f"{datetime.now(timezone.utc).astimezone(zone(cfg)):%Y-%m-%d}"
        new = entry(row["content"], day, row["model"])
        claimed = await store.write(lambda c: c.execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT DO NOTHING RETURNING v",
            (KEY + mid, new)).fetchone())
    else:
        claimed = await store.write(lambda c: c.execute(
            "DELETE FROM kv WHERE k=? RETURNING v", (KEY + mid,)).fetchone())
    ch = cfg.her
    p = ch.notebook / PATH
    body = p.read_text() if p.is_file() else None
    if claimed and on:
        await write_note(cfg, store, ch, p, PATH, added(body, claimed[0]))
    elif claimed and body is not None:
        await write_note(cfg, store, ch, p, PATH, removed(body, claimed[0]))
    return on
