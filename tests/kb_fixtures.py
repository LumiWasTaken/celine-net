"""Tiny, fake knowledge bases and source files for the importers, a fake embedder and
reranker, and the archive skill on them. No real chat data, ever."""
from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from celine.kb import KBs
from celine.kb.importers import ImportContext

T0 = 1_700_000_000_000          # 2023-11-14, ms
CTX = ImportContext(progress=lambda *a: None, cancelled=lambda: False)


ALEX_KEY = "491700000001@s.whatsapp.net"
GROUP_KEY = "12345-678@g.us"


def archive(kb, *, extra: bool = False) -> int:
    """The messages of `msgstore()` written into a chat archive as WhatsApp keys them:
    Alex's two 1:1 chats are one, the status update and the empty notice are left out.
    -> how many messages were new."""
    s = kb.sink()
    alex = s.person(ALEX_KEY, phone="+491700000001", push_name="alex ✨")
    bea = s.person("491700000002@s.whatsapp.net", phone="+491700000002")
    lid = s.person("88880001@lid")
    dm = s.chat(ALEX_KEY, kind="dm", person_key=ALEX_KEY)
    group = s.chat(GROUP_KEY, kind="group", name="Chess club")
    for cid, key, sender, ts, text, kind, media, reply in [
        (dm, "A1", alex, T0, "hey, the celine is finally afloat", "text", None, None),
        (dm, "A2", None, T0 + 60_000, "no way!", "text", None, None),
        (dm, "A3", alex, T0 + 120_000, "moved to my new number thing", "text", None, None),
        (group, "G1", alex, T0 + 180_000, "chess tonight?", "text", None, None),
        (group, "G2", bea, T0 + 240_000, "the clock was awful last time", "text", None, "G1"),
        (group, "G3", None, T0 + 300_000, "i'll bring a new clock", "text", None, None),
        (group, "G4", bea, T0 + 360_000, "ja ich komme später", "media",
         {"type": "voice"}, None),
        (group, "G5", alex, T0 + 420_000, "look at this", "media",
         {"type": "photo", "caption": "look at this"}, None),
        (group, "G6", bea, T0 + 480_000, "This message was deleted.", "system", None, None),
        (group, "G7", lid, T0 + 540_000, "who am i", "text", None, None),
    ] + ([(dm, "A4", alex, T0 + 900_000, "one more thing", "text", None, None)]
         if extra else []):
        s.message(cid, key, ts=ts, text=text, from_me=sender is None, sender=sender,
                  kind=kind, media=media, reply_key=reply)
    s.close()
    return s.added


def msgstore(path: Path, *, extra: bool = False) -> Path:
    """A minimal msgstore.db in the modern schema.

    - Alex: a 1:1 chat that exists twice, once under the phone JID and once under
      Alex's LID; jid_map says they are the same person.
    - "Chess club": a group where Alex writes from his LID and Bea from her phone JID.
    - One voice message with a transcription, one photo with a caption, one reply,
      one deleted message, one empty system row.
    `extra` adds one more message, to test that a re-import adds only it.
    """
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, agent INTEGER,
                          type INTEGER, raw_string TEXT, device INTEGER);
        CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER, sort_id INTEGER);
        CREATE TABLE lid_display_name (lid_row_id INTEGER, display_name TEXT, username TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
        CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER,
                              key_id TEXT, sender_jid_row_id INTEGER, timestamp INTEGER,
                              message_type INTEGER, text_data TEXT);
        CREATE TABLE message_quoted (message_row_id INTEGER, key_id TEXT);
        CREATE TABLE message_media (message_row_id INTEGER, media_caption TEXT,
                                    mime_type TEXT, media_name TEXT, file_path TEXT,
                                    media_duration INTEGER, raw_transcription_text TEXT);
    """)
    c.executemany("INSERT INTO jid (_id, user, server, raw_string) VALUES (?, ?, ?, ?)", [
        (1, "491700000001", "s.whatsapp.net", "491700000001@s.whatsapp.net"),   # Alex, phone
        (2, "99990001", "lid", "99990001@lid"),                                 # Alex, LID
        (3, "491700000002", "s.whatsapp.net", "491700000002@s.whatsapp.net"),   # Bea
        (4, "12345-678", "g.us", "12345-678@g.us"),                             # group
        (5, "status", "broadcast", "status@broadcast"),
        (6, "88880001", "lid", "88880001@lid"),                                 # unmapped
    ])
    c.execute("INSERT INTO jid_map VALUES (2, 1, 1)")
    c.execute("INSERT INTO lid_display_name VALUES (2, 'alex ✨', NULL)")
    c.executemany("INSERT INTO chat (_id, jid_row_id, subject) VALUES (?, ?, ?)", [
        (10, 1, None), (11, 2, None), (12, 4, "Chess club"), (13, 5, None)])
    rows = [
        # _id, chat, from_me, key, sender, ts, type, text
        (1, 10, 0, "A1", None, T0, 0, "hey, the celine is finally afloat"),
        (2, 10, 1, "A2", None, T0 + 60_000, 0, "no way!"),
        (3, 11, 0, "A3", None, T0 + 120_000, 0, "moved to my new number thing"),
        (4, 12, 0, "G1", 2, T0 + 180_000, 0, "chess tonight?"),
        (5, 12, 0, "G2", 3, T0 + 240_000, 0, "the clock was awful last time"),
        (6, 12, 1, "G3", None, T0 + 300_000, 0, "i'll bring a new clock"),
        (7, 12, 0, "G4", 3, T0 + 360_000, 2, None),                # voice
        (8, 12, 0, "G5", 2, T0 + 420_000, 1, "look at this"),      # photo, caption
        (9, 12, 0, "G6", 3, T0 + 480_000, 15, None),               # deleted
        (10, 12, 0, "G7", 6, T0 + 540_000, 0, "who am i"),         # unmapped LID
        (11, 12, 0, "G8", 3, T0 + 600_000, 7, None),               # empty notice
        (12, 13, 1, "S1", None, T0, 0, "a status update"),         # status: skipped
    ]
    if extra:
        rows.append((13, 10, 0, "A4", None, T0 + 900_000, 0, "one more thing"))
    c.executemany("INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    c.execute("INSERT INTO message_quoted VALUES (5, 'G1')")
    c.executemany("INSERT INTO message_media VALUES (?, ?, ?, ?, ?, ?, ?)", [
        (7, None, "audio/ogg; codecs=opus", None, None, 4, "ja ich komme später"),
        (8, "look at this", "image/jpeg", None, None, None, None)])
    c.commit()
    c.close()
    return path


def fake_embed(settings, texts):
    """Bag of words hashed into 64 dims, plus a small constant so no vector is zero."""
    out = []
    for t in texts:
        v = [0.01] * 64
        for w in re.findall(r"[a-z]{4,}", t.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
        n = math.sqrt(sum(x * x for x in v))
        out.append([x / n for x in v])
    return out


def embed_cfg(model="fake", **extra):
    """A config whose Embeddings task is one model on a fake Ollama (the router's probes
    answer as an idle server, tests/conftest.py)."""
    from celine.settings.registry import Settings
    s = Settings(providers={"p": {"kind": "ollama", "label": "P", "base_url": "http://x/v1",
                                  "models": {model: {"label": model,
                                                     "caps": ["embeddings"]}}}},
                 roles={"embeddings": {"chain": [f"p/{model}"]}})
    return SimpleNamespace(embeddings={"batch": 3, "dims": 64, "workers": 2, **extra},
                           settings=s, skills={}, r=s.value)


H = 3_600_000
T21 = int(datetime(2021, 3, 10, 18, 0).timestamp() * 1000)
T22 = int(datetime(2022, 7, 2, 20, 0).timestamp() * 1000)
T23 = int(datetime(2023, 11, 14, 10, 0).timestamp() * 1000)


def build(tmp_path) -> KBs:
    cfg = SimpleNamespace(data_dir=tmp_path, problems=[], kb={
        "whatsapp": {"kind": "chat", "title": "WhatsApp", "platform": "whatsapp"},
        "discord": {"kind": "chat", "title": "Discord", "platform": "discord"}})
    kbs = KBs(cfg, lambda ev: None)
    s = kbs.get("whatsapp").sink()
    alex = s.person("491700000001@s.whatsapp.net", phone="+491700000001", push_name="Alex")
    alexa = s.person("491700000009@s.whatsapp.net", phone="+491700000009",
                     push_name="Alexandra")
    bea = s.person("491700000002@s.whatsapp.net", phone="+491700000002", push_name="Bea")
    c1 = s.person("491700000003@s.whatsapp.net", push_name="Chris")
    s.person("491700000004@s.whatsapp.net", push_name="Chris")
    dm = s.chat("491700000001@s.whatsapp.net", person_key="491700000001@s.whatsapp.net")
    grp = s.chat("123-456@g.us", kind="group", name="Chess club")
    dm_c = s.chat("491700000003@s.whatsapp.net", person_key="491700000003@s.whatsapp.net")
    rows = [
        (dm, "d1", T21, alex, False, "hey, the celine is finally afloat"),
        (dm, "d2", T21 + 60_000, None, True, "no way! which sails?"),
        (dm, "d3", T21 + 120_000, alex, False, "the red ones, looks amazing"),
        (dm, "d4", T21 + 30 * 24 * H, alex, False, "unrelated: dinner on friday?"),
        (grp, "g1", T22, alex, False, "chess tonight?"),
        (grp, "g2", T22 + 60_000, bea, False, "the clock was awful last time"),
        (grp, "g3", T22 + 120_000, None, True, "i'll bring a new clock"),
        (grp, "g4", T22 + 180_000, bea, False, "haha ja"),
        (grp, "g5", T23, alexa, False, "my celine needs a new mast"),
        (grp, "g6", T23 + 60_000, bea, False, "Masten sind teuer"),
        (dm_c, "c1", T23 + 5 * H, c1, False, "celine regatta on sunday"),
    ]
    for chat, key, ts, sender, me, text in rows:
        s.message(chat, key, ts=ts, text=text, from_me=me, sender=sender)
    s.close()
    s = kbs.get("discord").sink()
    dalex = s.person("7", username="alex")
    s.person("8", username="sam", is_me=True)
    ch = s.chat("500", kind="channel", name="general", parent="Boat nerds")
    s.message(ch, "900", ts=T22 + 3 * H, text="anyone up for chess", sender=dalex)
    s.message(ch, "901", ts=T22 + 3 * H + 60_000, text="me, after dinner", from_me=True)
    s.close()
    return kbs


class WordReranker:
    """Scores a document by how many query words it contains."""

    def score(self, query, docs):
        words = [w for w in query.casefold().split() if len(w) > 2]
        return [float(sum(w in d.casefold() for w in words)) - 0.5 for d in docs]


def archive_skill(monkeypatch, kbs):
    from celine.core.services import Services
    from celine.kb.retrieve import rerank
    from celine.settings import general
    from celine.skills.archive import ChatArchive
    monkeypatch.setattr(rerank, "from_config", lambda cfg: WordReranker())
    cfg = SimpleNamespace(user="sam", substitute=lambda s: s, embeddings={}, settings=None,
                          r=lambda key: general.value({}, key))
    skill = ChatArchive(cfg)
    skill.app = Services(kbs=kbs)
    monkeypatch.setattr(skill, "_embedder", lambda: (None, "", None))
    return skill
