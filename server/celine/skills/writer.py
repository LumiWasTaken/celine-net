"""The agent's words in its own voice (writer__draft): one request on the writer role (Settings,
Models, Writing) with who it is (its persona, identity.md), how it talks (its talk,
her.py, and the "Love this" samples: conversations/samples.py), its brief
and what it answers; the text alone comes back.

It runs in its turn on exactly the role's first model: when that model fails, the call
fails with its error, no other model is tried. Thinking off (a model that cannot stop
thinking thinks least, settings/thinking.py); its voice bans go into the request
(settings/voice.py)."""
from __future__ import annotations

from typing import Annotated, Literal

import math

from ..backends.base import BackendError
from .. import prompts
from ..backends.queue import Priority
from ..settings.thinking import OFF
from ..turns.compact import complete
from . import ToolContext, ToolResult, guide, tool, described
from .notebook import carried_text, read_note

ROLE = "writer"
KINDS = {"post": "a post: its title on the first line, a blank line, then the body",
         "comment": "a comment", "message": "a message"}
# what the writer is told: skills/writer.md


def voice_notes(cfg) -> list[tuple[str, str]]:
    """How it talks: its talk, then it "Love this" samples when it has any."""
    from ..conversations.samples import PATH
    out = [("how you talk", cfg.her.talk_text)]
    if samples := carried_text(read_note(cfg.her.notebook, PATH) or ""):
        out.append((PATH, samples))
    return out


def messages(cfg, brief: str, kind: str, context: str) -> list[dict]:
    """The request: its persona, identity.md, its talk and samples in the system prompt, the
    brief and its context as the user message."""
    root = cfg.her.notebook
    parts = [prompts.fill(cfg, __file__, "writer.md"), cfg.substitute(cfg.her.persona).strip()]
    if identity := read_note(root, "identity.md"):
        parts.append("identity.md:\n" + cfg.substitute(identity))
    parts += [f"### {rel}\n{cfg.substitute(text)}" for rel, text in voice_notes(cfg)]
    ask = f"Write {KINDS[kind]}.\n\nBrief: {brief.strip()}"
    if context.strip():
        ask += f"\n\nWhat it answers, where it goes:\n{context.strip()}"
    return [{"role": "system", "content": "\n\n".join(p for p in parts if p)},
            {"role": "user", "content": ask}]


class Writer:
    name = "writer"
    guide = guide("writer")
    description = described()

    def __init__(self, cfg):
        self.cfg = cfg
        self.app = None

    @tool()
    async def draft(
        self,
        ctx: ToolContext,
        brief: Annotated[str, "What you want to say: the point, the mood, how long."],
        kind: Annotated[Literal["post", "comment", "message"],
                        "post: a title line, then the body. comment or message: the text."],
        context: Annotated[str, "What it answers or where it goes: the thread or comment "
                                "you reply to, where it goes, who you write to."] = "",
    ) -> ToolResult:
        """Write one post, comment or message in your own voice, by the Writing model.
        Example: writer__draft(brief="thank them for the photos, short",
        kind="message", context="a message from a friend: 'here are the trip photos'").
        Returns: the text alone, ready to use."""
        if not brief.strip():
            return ToolResult(content="The brief is empty: say what to write.",
                              is_error=True)
        llm = self.app.llm if self.app is not None else None
        if llm is None:
            return ToolResult(content="The app's model layer is not running.", is_error=True)
        qb = llm.bind(None, role=ROLE, priority=llm.turn_priority.get(ctx.turn_id,
                                                                      Priority.AGENT),
                      conversation_id=ctx.conversation_id, turn_id=ctx.turn_id,
                      thinking=OFF, fallback=False)
        try:
            text = (await complete(qb, messages(self.cfg, brief, kind, context), math.inf,
                                   errors=True) or "").strip()
        except BackendError as e:
            return ToolResult(content=f"The Writing model failed: {str(e)[:300]}",
                              is_error=True)
        model = getattr(qb.answered, "model", "")
        if not text:
            return ToolResult(content=f"The Writing model ({model}) answered nothing.",
                              is_error=True)
        return ToolResult(content=text, display={"kind": kind, "model": model,
                                                 "chars": len(text)})
