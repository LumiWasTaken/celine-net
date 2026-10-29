"""voice__send: her text spoken by the Read aloud model, an audio file on her message;
with Read aloud off it fails and says so."""
from __future__ import annotations

import pytest

from chat_fixtures import user_says

from celine import attachments, speech
from celine.boot import load
from celine.conversations import store as conversations, tree
from celine.skills import ToolContext
from celine.skills.voice import Voice
from celine.turns import store as turns

pytestmark = pytest.mark.anyio


async def _ctx(store, cfg) -> tuple[ToolContext, str]:
    cid = await conversations.create(store)
    _, tid, _ = await user_says(store, cid, "tell me")
    mid = await tree.start_assistant_message(store, cid, None, tid)
    await turns.record_tool_calls(store, cid, mid, [{"id": "c1", "name": "voice__send",
                                                     "arguments": "{}"}])
    return ToolContext(conversation_id=cid, turn_id=tid, tool_call_id="c1", store=store,
                       config=cfg, emit=None), mid


async def test_a_voice_note_lands_on_her_message(store, monkeypatch):
    cfg = load()
    cfg.settings.features["speech_output"] = True
    said = []

    async def synthesize(_cfg, text, voice):
        said.append((text, voice))
        return b"RIFF....WAVE", "audio/wav"
    monkeypatch.setattr(speech, "synthesize", synthesize)
    ctx, mid = await _ctx(store, cfg)
    out = await Voice(cfg).send(ctx, text="  The build is green.  ")
    assert not out.is_error, out.content
    assert said == [("The build is green.", "")], "spoken as written"
    row = await attachments.get(store, out.display["attachments"][0]["id"])
    assert (row["message_id"], row["kind"], row["filename"]) == (mid, "audio", "voice-note.wav")
    assert out.content == f"Voice note on your message (12 bytes), short id {row['id'][:8]}."


async def test_it_fails_with_the_reason(store, monkeypatch):
    cfg = load()
    cfg.settings.features["speech_output"] = False
    ctx, _ = await _ctx(store, cfg)
    out = await Voice(cfg).send(ctx, text="hi")
    assert out.is_error and out.content == "Read aloud is off in Settings, Features."
    cfg.settings.features["speech_output"] = True
    long = await Voice(cfg).send(ctx, text="x" * (speech.MAX_TEXT + 1))
    assert long.is_error and long.content.startswith("A voice note is 1 to 900 characters")
