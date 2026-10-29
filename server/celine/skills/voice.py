"""Voice notes: her text spoken by the Read aloud role's model (speech.py), an audio file
on her message, played where the message shows. From elsewhere she sends one on with
intercom__send(attachments=...)."""
from __future__ import annotations

import mimetypes
from typing import Annotated

from .. import attachments, speech
from ..errors import CelineError
from ..settings import features
from . import ToolContext, ToolResult, described, guide, tool


class Voice:
    name = "voice"
    guide = guide("voice")
    description = described()

    app = None      # the running app (core/services.py), from skills.build

    def __init__(self, cfg):
        self.cfg = cfg

    @tool(read_only=False, destructive=False, open_world=True)
    async def send(
        self,
        ctx: ToolContext,
        text: Annotated[str, "What you say, spoken exactly as written: plain text only, "
                             "no emojis, no actions, no formatting."],
    ) -> ToolResult:
        """Send {{username}} a voice note: your text spoken by the Read aloud model, an
        audio file on your message. The text is spoken exactly as written, so it is plain
        text only: no emojis, no actions, no formatting.
        Returns: its size and its short id."""
        if not features.on(self.cfg, "speech_output"):
            return ToolResult(content="Read aloud is off in Settings, Features.",
                              is_error=True)
        text = text.strip()
        if not text or len(text) > speech.MAX_TEXT:
            return ToolResult(content=f"A voice note is 1 to {speech.MAX_TEXT} characters; "
                                      f"this is {len(text)}.", is_error=True)
        if ctx.store is None:
            return ToolResult(content="Only a conversation can take a voice note.",
                              is_error=True)
        try:
            audio, mime = await speech.synthesize(self.cfg, text, "")
        except CelineError as e:
            return ToolResult(content=e.message, is_error=True)
        name = "voice-note" + (mimetypes.guess_extension(mime) or ".wav")
        row = await attachments.save_generated(self.cfg, ctx.store, audio, name, mime,
                                               ctx.tool_call_id)
        short = row["id"][:attachments.SHORT]
        return ToolResult(content=f"Voice note on your message ({attachments.size(row['bytes'])}), "
                                  f"short id {short}.",
                          display={"attachments": [{k: row[k] for k in (
                              "id", "sha256", "filename", "media_type", "bytes")}]})
