"""The feed consumer: canonical records from a connector into a chat archive; a failing
feed backs off."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from celine.kb import connectors as conn_mod, feed
from celine.kb.chat import ChatKB
from celine.kb.feed import Consumer, KeyRefused, current_seq
from celine.kb.importers import Cancelled, Failed, ImportContext, Unreachable

RECS = [json.loads(line) for line in (Path(__file__).parent / "data" / "feed" /
                                      "discord.ndjson").read_text().splitlines() if line]


def _kb(tmp_path: Path, kid: str = "discord") -> ChatKB:
    return ChatKB(id=kid, title=kid.title(), platform=kid, path=tmp_path / f"{kid}.db")


def _q(kb: ChatKB, sql: str):
    return kb.file.read_sync(lambda c: [tuple(r) for r in c.execute(sql).fetchall()])


def test_apply_is_idempotent_keeps_old_text_and_a_delete_leaves_a_tombstone(tmp_path):
    kb = _kb(tmp_path)
    early = Consumer(kb).apply([r for r in RECS if r["seq"] <= 17])
    assert early.pending == 2, "an edit and a delete before their message wait for it"
    assert Consumer(kb).apply(RECS).pending == 0
    count = "SELECT (SELECT COUNT(*) FROM messages), (SELECT COUNT(*) FROM message_edits)"
    counts = _q(kb, count)
    kb.file.write_sync(lambda c: c.execute("UPDATE kv SET value = '0' WHERE key = 'feed_seq'"))
    again = Consumer(kb).apply(RECS)                    # a replay from a fresh cursor
    assert (again.added, again.edits, again.deleted) == (0, 0, 0) and _q(kb, count) == counts
    assert _q(kb, "SELECT m.text, e.old_text FROM messages m JOIN message_edits e "
                  "ON e.message_id = m.id WHERE m.key = 'm2'") == \
        [("almost, new spark plugs tomorrow", "almost, new plugs tomorrow")]
    [(text, deleted_at)] = _q(kb, "SELECT text, deleted_at FROM messages WHERE key = 'm5'")
    assert text == "" and deleted_at is not None, "a deleted message keeps no text"
    assert _q(kb, "SELECT text FROM messages WHERE key = 'm20'") == [("fixed typo",)]


def test_a_deleted_message_takes_its_unused_file_with_it(tmp_path):
    from celine.kb.media import media_dir
    kb = _kb(tmp_path)
    Consumer(kb).apply([r for r in RECS if r["seq"] <= 7])
    sha = RECS[6]["message"]["media"]["files"][0]["sha256"]
    media_dir(kb).mkdir(parents=True, exist_ok=True)
    (media_dir(kb) / sha).write_bytes(b"png")
    Consumer(kb).apply([{"seq": 8, "kind": "delete", "chat_key": "c300",
                         "message_keys": ["m6"], "ts": 1700000600000}])
    assert _q(kb, "SELECT text, media FROM messages WHERE key = 'm6'") == [("", None)]
    assert not (media_dir(kb) / sha).exists()
    Consumer(kb).apply(RECS[6:7], cursor=False)          # the message again: stays gone
    assert _q(kb, "SELECT media FROM messages WHERE key = 'm6'") == [(None,)]


def test_a_bad_record_is_skipped_and_a_failed_batch_moves_no_cursor(tmp_path, monkeypatch):
    kb = _kb(tmp_path)
    got = Consumer(kb).apply(RECS[:3] + [{"seq": 4, "kind": "message", "chat": {"key": "x"}},
                                         {"seq": 5, "kind": "nonsense"}] + RECS[5:10])
    assert got.skipped == 2 and got.seq == 10
    real = feed._Writer.apply

    def boom(self, r, out):
        if r["seq"] == 15:
            raise RuntimeError("disk on fire")
        return real(self, r, out)
    monkeypatch.setattr(feed._Writer, "apply", boom)
    with pytest.raises(RuntimeError):
        Consumer(kb).apply(RECS)
    assert current_seq(kb) == 10
    monkeypatch.setattr(feed._Writer, "apply", real)
    assert Consumer(kb).apply(RECS).seq == 30 and current_seq(kb) == 30


def _stream(lines: list[str], seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(after=request.url.params.get("after"),
                    auth=request.headers.get("authorization"))
        body = "".join(line + "\n" for line in lines).encode()
        return httpx.Response(200, content=iter([body[i:i + 97]
                                                 for i in range(0, len(body), 97)]))
    return httpx.MockTransport(handler)


def test_the_stream_applies_from_the_cursor_and_a_cancel_keeps_what_was_applied(tmp_path):
    ctx = ImportContext(progress=lambda *a: None, cancelled=lambda: False)
    kb = _kb(tmp_path)
    Consumer(kb).apply(RECS[:5])
    seen: dict = {}
    out = Consumer(kb).run(ctx, "http://link", "k1", batch=7,
                           transport=_stream([json.dumps(r) for r in RECS[5:]], seen))
    assert seen == {"after": "5", "auth": "Bearer k1"} and out["seq"] == current_seq(kb) == 30
    with pytest.raises(Failed):
        Consumer(kb).run(ctx, "http://link", "bad",
                         transport=httpx.MockTransport(lambda r: httpx.Response(403)))

    other, lines = _kb(tmp_path, "other"), iter(range(100))
    with pytest.raises(Cancelled):
        Consumer(other).run(ImportContext(progress=lambda *a: None,
                                          cancelled=lambda: next(lines) >= 12),
                            "http://link", None, batch=5,
                            transport=_stream([json.dumps(r) for r in RECS], {}))
    assert current_seq(other) == 10


def test_a_failing_feed_backs_off_and_a_refused_key_waits(tmp_path, monkeypatch):
    kb = ChatKB(id="d", title="Discord", platform="discord", path=tmp_path / "d.db")
    answer = [503]
    c = conn_mod.Connector("discord", "http://x/mcp", {}, kb, sync_transport=httpx.MockTransport(
        lambda r: httpx.Response(answer[0])))
    ctx = ImportContext(progress=lambda *a: None, cancelled=lambda: False)
    last = {"state": "unreachable", "finished_at": 0}
    for wait in (10_000, 60_000, 600_000, 600_000):
        with pytest.raises(Unreachable):
            c.feed(ctx)
        monkeypatch.setattr(conn_mod, "now_ms", lambda w=wait: w - 1)
        assert not c.feed_due(last)
        monkeypatch.setattr(conn_mod, "now_ms", lambda w=wait: w)
        assert c.feed_due(last)
    answer[0] = 401
    with pytest.raises(KeyRefused):
        c.feed(ctx)
    monkeypatch.setattr(conn_mod, "now_ms", lambda: 10 ** 12)
    assert not c.feed_due({"state": "failed", "finished_at": 0})
    assert c.feed_due({"state": "done", "finished_at": 0})
