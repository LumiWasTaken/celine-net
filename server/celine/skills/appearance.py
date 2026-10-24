"""The agent's portrait, set by it (her.set_portrait): one of its images, or one the user attached here (by its short id, as image_generator's edits
take it), or an image in Documents (by its id, path or title, as user_documents__read
takes it)."""
from __future__ import annotations

import re
from typing import Annotated

import anyio

from .. import attachments
from .. import her
from . import ToolContext, ToolResult, guide, tool, described
from ..core.event_types import HER_CHANGED


class Appearance:
    name = "appearance"
    guide = guide("appearance")
    description = described()

    app = None      # the running app (core/services.py), from skills.build

    def __init__(self, cfg):
        self.cfg = cfg

    @tool(read_only=False, destructive=False)
    async def set_portrait(
        self,
        ctx: ToolContext,
        image: Annotated[str, "The image: its short id, like f5ef648c, from an image "
                              "result or image_generator__list_images, or a document's "
                              "id, path or title."],
    ) -> ToolResult:
        """Make an image your portrait: the face {{username}} sees beside your messages
        and everywhere you speak in the app. It replaces the one before.
        Returns: which image it is now."""
        got = await self._image(ctx, image)
        if isinstance(got, str):
            return ToolResult(content=got, is_error=True)
        data, name = got
        try:
            await her.set_portrait(ctx.store, self.cfg, data)
        except ValueError as e:
            return ToolResult(content=f"{name} cannot be your portrait. {e}",
                              is_error=True)
        if ctx.store is not None:
            ctx.store.emit_global({"type": HER_CHANGED})
        return ToolResult(content=f"Your portrait is now {name}.",
                          display={"portrait": her.portrait_view(self.cfg.her)})

    async def _image(self, ctx: ToolContext, ref: str) -> tuple[bytes, str] | str:
        """(the image's bytes, how to name it), or the sentence why there is none."""
        ref = ref.strip()
        row = await attachments.her_image(ctx.store, ref, ctx.conversation_id,
                                          self.cfg.user)
        if not isinstance(row, str):
            data = attachments.blob_path(self.cfg, row["sha256"]).read_bytes()
            return data, f"image {row['id'][:attachments.SHORT]}"
        kbs = self.app.kbs if self.app is not None else None
        k = kbs.documents() if kbs else None
        it = await anyio.to_thread.run_sync(k.find_item, ref) if k is not None else None
        if it is None:
            if re.fullmatch(r"[0-9a-fA-F]{4,32}", ref):
                return row      # a short id, but not of an image it may use
            return f"There is no document called {ref}. user_documents__list shows them."
        if it["kind"] != "image":
            return f"The document {it['path']} is not an image."
        try:
            data = await anyio.to_thread.run_sync(
                lambda: k.resolve(it["path"], must_exist=True).read_bytes())
        except (OSError, ValueError):
            return f"The document {it['path']} cannot be read."
        return data, f"the document {it['path']}"
