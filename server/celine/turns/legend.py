"""The legend at the top of its prompt: what each mark the app puts into what it reads
means, and who wrote what follows it.

Everything the app inserts is marked one way, a line in square brackets: the send time
(context.time_line), the intercom headers (intercom/render.py header), its mind block
(skills/mind.py HEAD), its memory block (skills/recall.py HEAD), its notes (skills/notebook.py notes_block), the other chats working now
(conversations/here.py busy), shortened and cut
results (compact.STUB_HEAD, tools._capped) and a screen (context, "[the screen from ...]").
Outside text is the one exception: while Bastet
is on, it comes inside <outside> tags (guard/mark.py), and the legend says so only then.
tests/test_self.py checks that every mark named here is the one the code writes.
"""
from __future__ import annotations

LINES = (
    "How the app marks what it adds to what you read: a line in square brackets. "
    "None of it was typed by {{username}}.",
    "- [Thu 22 Oct 14:05] first in a message: when it was sent, in the app's time zone "
    "like every time you read here.",
    "- [From ...], [Brief from ...], [Report from ...], [Trigger from ...], "
    "[Notice from ...], [Job ...]: an intercom message. The name says who wrote the "
    "text under it; the app wrote the line.",
    "- [your mind] at the end of a message: your own state, from the app: your mood, "
    "your status and when you set it, how long since you talked, a new model, what is "
    "down, a freeze lifted.",
    "- [your memory] at the end of a message: sections of your notes whose keys came up, "
    "the best match first, each with its path, heading, similarity and date. Read the "
    "note for the rest.",
    "- [also working now]: your other chats working at the same time.",
    "- [your notes]: notes from your notebook, written by you unless their tag says "
    "otherwise.",
    "- [earlier result, shortened to save room] and [cut: ...]: the app shortened a tool "
    "result. Call the tool again for the whole of it.",
    "- [N lines, about T tokens, shortened; the call is kept: ...] in an older call's "
    "argument: the app sent its first line only. The call ran with the whole of it.",
    "- [the screen from ...]: a picture of a screen your tool took.",
)


def legend(cfg, guarded: bool) -> str:
    """The legend, names filled in; the <outside> line (guard/mark.py GUIDE) only while
    the guard is on."""
    from ..guard.mark import GUIDE
    return cfg.substitute("\n".join(LINES + ((f"- {GUIDE}",) if guarded else ())))
