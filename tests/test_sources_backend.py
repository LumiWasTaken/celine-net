"""sources-backend: an upsert on a row waiting for search."""
from __future__ import annotations

from celine.kb.chat import ChatKB
from celine.kb.feed import Consumer, current_seq

from kb_fixtures import ALEX_KEY as ALEX, T0, archive


class Feed:
    def __init__(self, kb: ChatKB):
        self.kb, self.consumer = kb, Consumer(kb)

    def apply(self, records: list[dict]):
        seq = current_seq(self.kb)
        return self.consumer.apply([{**r, "seq": seq + i + 1} for i, r in enumerate(records)])


def msg(chat: str, mid: str, ts: int, text: str = "x", *, sender: str | None = None,
        from_me: bool = False) -> dict:
    kind = "group" if chat.endswith("@g.us") else "dm"
    c = {"key": chat, "kind": kind, **({"peer": {"key": chat}} if kind == "dm" else {})}
    m = {"key": mid, "ts": ts, "from_me": from_me, "type": "text", "text": text}
    if not from_me:
        m["sender"] = {"key": sender or chat}
    return {"kind": "message", "chat": c, "message": m}


def test_an_upsert_on_a_dirty_row_does_not_fail(tmp_path):
    """instant.sql's triggers used INSERT OR IGNORE, which an upsert overrides."""
    kb = ChatKB(id="whatsapp", title="WhatsApp", platform="whatsapp",
                path=tmp_path / "kb" / "whatsapp.db", options={})
    archive(kb)
    # A1 arriving from the link with a sender updates the row while it is still
    # waiting in search_dirty
    kb.file.write_sync(lambda c: c.execute("UPDATE messages SET sender_id = NULL "
                                           "WHERE key = 'A1'"))
    got = Feed(kb).apply([msg(ALEX, "A1", T0, "hey, the celine is finally afloat")])
    assert got.added == 0 and got.records == 1
