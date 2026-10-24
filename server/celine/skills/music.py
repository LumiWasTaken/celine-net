"""Music with ComfyUI: one batch per call (comfyui/batches.py), the same way as images.

The agent's call starts the batch in the app's task group and returns at once with one slot
per track; the batch fills the slots in place as each track is made. It is no job, so
nothing wakes it. `wait` holds its turn for the end.
"""
from __future__ import annotations

from typing import Annotated, Required, TypedDict

from ..comfyui import batches as batches_mod, presets as presets_mod
from . import ToolContext, ToolResult, guide, tool, described
from .images import batch_generate, batch_read, batch_stop, role_presets

SHORT = batches_mod.SHORT


class Item(TypedDict, total=False):
    style: Required[Annotated[str, "Genre, instruments, tempo, space and mood, comma "
                                   "separated (the guide shows how)."]]
    lyrics: Annotated[str, "With section tags like [Verse 1] and [Chorus]. Empty for "
                           "an instrumental."]
    title: Annotated[str, "A short name for the track."]
    instrumental: Annotated[bool, "True (the default): no vocals. False: a song with "
                                  "the lyrics."]
    duration: Annotated[int, "The most the track may last, in seconds. Empty: the "
                             "preset's default."]


def item_line(i: dict) -> str:
    n = i["index"] + 1
    if i["state"] == "failed":
        return f"{n}. failed: {i['error']}"
    if i["state"] == "stopped":
        return f"{n}. stopped before it was made."
    what = "instrumental" if i["instrumental"] else "song"
    if i["state"] != "done":
        return f"{n}. {i['title']} ({what}), seed {i['seed']}: {i.get('note') or i['state']}."
    return (f"{n}. track {i['attachment']['id'][:SHORT]}: {i['title']} ({what}), seed "
            f"{i['seed']}, {i.get('seconds', 0):.0f} s.")


def batch_text(b: dict, user: str, *, started: bool, chat: bool = True) -> str:
    """A batch as it reads it; started in a chat (`chat`), {user} hears it there."""
    items = b["items"]
    made = sum(i["state"] == "done" for i in items)
    if started:
        hears = f" {user} hears each one in this conversation when it is done." if chat \
            else ""
        head = (f"Batch {b['id'][:SHORT]} started: {len(items)} track"
                f"{'s' if len(items) != 1 else ''}, made one after the other, a few "
                f"minutes each.{hears} Nothing reports back to you: music_generator__read "
                f"shows the batch.")
    else:
        head = f"Batch {b['id'][:SHORT]} is {b['state']}: {made} of {len(items)} made."
    return "\n".join([head] + [item_line(i) for i in items])


class MusicGenerator:
    name = "music_generator"
    guide = guide("music_generator")
    description = described()

    app = None      # the running app (core/services.py), from skills.build

    def __init__(self, cfg):
        self.cfg = cfg

    def _presets(self) -> list[presets_mod.Preset]:
        return role_presets(self.cfg, "music")

    def _text(self, b: dict, started: bool, chat: bool = True) -> str:
        return batch_text(b, self.cfg.user, started=started, chat=chat)

    @property
    def status(self) -> str:
        return "up" if self._presets() else "unset"

    @property
    def error(self) -> str | None:
        if self.status == "up":
            return None
        return (f"No preset is set. {self.cfg.user} chooses presets in Settings, Images & "
                f"Music.")

    @property
    def docs(self) -> dict[str, str]:
        """The first preset of the role, from its file, shown after the guide."""
        found = self._presets()
        return {"presets": presets_mod.docs(found[0], True)} if found else {}

    @tool(read_only=False, destructive=False, open_world=True, timeout=batches_mod.WAIT_MAX)
    async def generate(
        self,
        ctx: ToolContext,
        items: Annotated[list[Item], "The tracks, in order."],
        count: Annotated[int, "How many tracks of each item, each with its own seed. "
                              "Default 1."] = 1,
        wait: Annotated[bool, "True: your turn waits until the batch is done, as long "
                              "as its time allows. Default false: the call returns at "
                              "once."] = False,
    ) -> ToolResult:
        """Make music with ComfyUI: instrumental tracks, or songs with lyrics. Load the
        guide first: it has the style rules. The call returns at once with the batch id
        and one slot per track. The tracks are made one after the other, a few minutes
        each, and appear in this conversation as each is done; in a chat {{username}}
        hears them there. Nothing reports back: music_generator__read shows the batch later,
        music_generator__stop stops it. Returns: the batch id and each slot."""
        from .places import place_of
        chat = await ctx.store.read(lambda c: place_of(c, ctx.conversation_id)) == "chat"
        return await batch_generate(self.app, ctx, "music", items, count, wait,
                                    lambda b, s: self._text(b, s, chat))

    @tool()
    async def read(
        self,
        ctx: ToolContext,
        batch: Annotated[str, "The batch id from generate, like 3f2a9c1e."],
    ) -> ToolResult:
        """A batch's state: each slot, waiting, being made, made (with its track's
        short id), failed (with why) or stopped. Returns: one line per track."""
        return await batch_read(ctx, "music", batch, self._text)

    @tool(read_only=False, destructive=False)
    async def stop(
        self,
        ctx: ToolContext,
        batch: Annotated[str, "The batch id from generate, like 3f2a9c1e."],
    ) -> ToolResult:
        """Stop a batch: the track being made is interrupted, the ones after it are not
        made. What was made stays. Returns: the batch."""
        return await batch_stop(self.app, ctx, "music", batch, self._text)
