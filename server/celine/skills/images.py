"""Images with ComfyUI: one batch per call (comfyui/batches.py), new images and edits.

The agent's call starts the batch in the app's task group and returns at once with one slot
per image; the batch fills the slots in place as each image is made. It is no job, so
nothing wakes it. `wait` holds its turn for the end. It never sees the images: it gets
the preset, the size, the seed and each image's short id. An edit starts only from an
image it made (an attachment of its own message, in any conversation) or one the user
attached to this conversation; never from the Messages archive or Documents.
"""
from __future__ import annotations

import types
import typing
from collections.abc import Callable
from typing import Annotated, Literal, Required, TypedDict

from .. import attachments
from ..comfyui import batches as batches_mod, presets as presets_mod
from . import ToolContext, ToolResult, guide, tool, described

SHORT = batches_mod.SHORT


def short(aid: str) -> str:
    return aid[:SHORT]


class Item(TypedDict, total=False):
    prompt: Required[Annotated[str, "What to draw, written for the preset (the guide "
                                    "shows how). With source: what to change."]]
    source: Annotated[str, "An image to edit: its short id, like f5ef648c, from a "
                           "result or list_images, or portrait:<name> for someone's "
                           "portrait. Empty: a new image."]
    source2: Annotated[str, "A second image for the edit, as source: another person, "
                            "an outfit, a place. Only a preset that takes several images "
                            "uses it (the guide says which)."]
    source3: Annotated[str, "A third image, as source2."]
    source4: Annotated[str, "A fourth image, as source2."]
    preset: Annotated[str, "A preset id. Left out: the first preset for a new image or an "
                           "edit, or the one a model or LoRA you give belongs to."]
    negative: Annotated[str, "What to keep out of the image. Only a preset that reads "
                             "a negative prompt uses it."]
    width: Annotated[int, "In pixels. Empty: the preset's size, or for an edit the "
                          "image's shape."]
    height: Annotated[int, "In pixels. Empty: as for width."]
    seed: Annotated[int, "The same seed and prompt give the same image. Empty: a new "
                         "one."]
    model: Annotated[str, "A model id from list_presets. Empty: the preset's own."]
    loras: Annotated[list[str], "LoRA ids from list_presets, each optionally with a "
                                "strength after a colon, like \"style/my_style_v1:0.8\"."]
    variant: Annotated[str, "A variant of the preset, for one that has variants (the "
                            "guide lists them per preset, like direct or expand). Never "
                            "a preset id: that goes in preset. Empty: its default."]


def item_line(i: dict) -> str:
    """One slot of a batch, as it reads it."""
    n = i["index"] + 1
    warned = "".join(f" Note: {w}" for w in i.get("warnings") or [])
    if i["state"] == "failed":
        return f"{n}. failed: {i['error']}{warned}"
    if i["state"] == "stopped":
        return f"{n}. stopped before it was made.{warned}"
    p = presets_mod.of("images").get(i.get("preset", ""))
    label = p.label if p else i.get("preset")
    att = i.get("attachment") or {}
    size = f"{att.get('width', i.get('width'))}x{att.get('height', i.get('height'))}"
    if i["state"] != "done":
        line = f"{n}. {label}, {size}, seed {i['seed']}: {i.get('note') or i['state']}."
        if i.get("mode"):
            line += f" Variant {i['mode']}."
        if i.get("model"):
            line += f" Model {i['model']}."
        if i.get("loras"):
            line += f" LoRAs {', '.join(i['loras'])}."
        return line + warned
    line = f"{n}. image {short(att['id'])}: {label}, {size}, seed {i['seed']}"
    if i.get("seconds") is not None:
        line += f", {i['seconds']:.0f} s"
    line += "."
    if i.get("source"):
        line += f" Edited from {_sources(i)}."
    if i.get("model"):
        line += f" Model {i['model']}."
    if i.get("loras"):
        line += f" LoRAs {', '.join(i['loras'])}."
    if i.get("triggers"):
        line += f" Trigger words added: {', '.join(i['triggers'])}."
    if i.get("mode"):
        line += f" Variant {i['mode']}."
    return line + warned


def _src(aid: str) -> str:
    return aid if aid.startswith("portrait:") else short(aid)


def _sources(i: dict) -> str:
    """An edit's images: "a", "a and b", "a, b and c"."""
    ids = [_src(i[k].get("id") or "") for k in presets_mod.SOURCES if i.get(k)]
    return ", ".join(ids[:-1]) + " and " + ids[-1] if len(ids) > 1 else ids[0]


def batch_text(b: dict, user: str, *, started: bool, chat: bool = True) -> str:
    """A batch as it reads it; started in a chat (`chat`), {user} sees it there."""
    items = b["items"]
    made = sum(i["state"] == "done" for i in items)
    if started:
        sees = f" {user} sees each one in this conversation when it is done." if chat \
            else ""
        head = (f"Batch {short(b['id'])} started: {len(items)} image"
                f"{'s' if len(items) != 1 else ''}, made one after the other.{sees} "
                f"Nothing reports back to you: image_generator__read shows the batch.")
    else:
        head = f"Batch {short(b['id'])} is {b['state']}: {made} of {len(items)} made."
    lines = [head] + [item_line(i) for i in items]
    return "\n".join(lines)


# ---- what images and music share: the role's presets and a batch's tools ----------------

def role_presets(cfg, role: str) -> list[presets_mod.Preset]:
    """The presets of a generation role ("images", "music"), in its order."""
    s = getattr(cfg, "settings", None)
    found = presets_mod.of(role)
    ids = dict.fromkeys(s.split(r)[1] for r in s.chain(presets_mod.ROLES[role])) \
        if s is not None else {}
    return [found[i] for i in ids if i in found]


async def batch_generate(app, ctx: ToolContext, role: str, items, count: int, wait: bool,
                         text: Callable[[dict, bool], str], **kw) -> ToolResult:
    try:
        b = await app.batches.create(role, items, count=count,
                                     conversation_id=ctx.conversation_id,
                                     tool_call_id=ctx.tool_call_id, **kw)
    except batches_mod.TooMany as e:
        return ToolResult(content=str(e), is_error=True)
    b = await app.batches.begin(b, wait=wait, time_left=ctx.time_left)
    # the call's display is the batch's own: it fills it in place (batches._publish)
    return ToolResult(content=text(b, b["state"] == "running" and not wait),
                      is_error=b["state"] != "running" and not any(
                          i["state"] == "done" for i in b["items"]))


async def batch_read(ctx: ToolContext, role: str, batch: str,
                     text: Callable[[dict, bool], str]) -> ToolResult:
    try:
        b = await batches_mod.find(ctx.store, role, batch)
    except presets_mod.ImageError as e:
        return ToolResult(content=str(e), is_error=True)
    return ToolResult(content=text(b, False), display=batches_mod.view(b))


async def batch_stop(app, ctx: ToolContext, role: str, batch: str,
                     text: Callable[[dict, bool], str]) -> ToolResult:
    try:
        b = await batches_mod.find(ctx.store, role, batch)
    except presets_mod.ImageError as e:
        return ToolResult(content=str(e), is_error=True)
    if not await app.batches.stop(b["id"]):
        return ToolResult(content=f"Batch {short(b['id'])} is not running: it is "
                                  f"{b['state']}.")
    b = await batches_mod.get(ctx.store, b["id"])
    return ToolResult(content=text(b, False), display=batches_mod.view(b))


class ImageGenerator:
    name = "image_generator"
    guide = guide("image_generator")
    description = described()

    app = None      # the running app (core/services.py), from skills.build

    def __init__(self, cfg):
        self.cfg = cfg
        if ids := tuple(p.id for p in self._presets()):
            self.generate = types.MethodType(_with_presets(ids), self)

    def _presets(self) -> list[presets_mod.Preset]:
        return role_presets(self.cfg, "images")

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
        """The presets of the role, from their files, shown after the guide."""
        found = self._presets()
        out = ["The presets, in order. Without `preset`, an item takes the first one for "
               "its task (a new image, or an edit with `source`)."]
        for task in ("generate", "edit"):
            of_task = [p for p in found if p.task == task]
            out += [presets_mod.docs(p, n == 0) for n, p in enumerate(of_task)]
        return {"presets": "\n\n".join(out)}

    @tool(read_only=False, destructive=False, open_world=True, timeout=batches_mod.WAIT_MAX)
    async def generate(
        self,
        ctx: ToolContext,
        items: Annotated[list[Item], "The images, in order: each a new image from its "
                                     "prompt, or with source an edit."],
        count: Annotated[int, "How many images of each item, each with its own seed. "
                              "Default 1."] = 1,
        wait: Annotated[bool, "True: your turn waits until the batch is done, as long "
                              "as its time allows. Default false: the call returns at "
                              "once."] = False,
    ) -> ToolResult:
        """Make images with ComfyUI: new ones from a prompt, or edits of an image (an
        item with `source`). Load the guide first: it has each preset's prompt rules.
        The call returns at once with the batch id and one slot per image. The images
        are made one after the other and appear in this conversation as each is done;
        {{username}} sees them there. You do not see them, and nothing reports back
        by itself: image_generator__read shows the batch. image_generator__stop stops a batch.
        Your prompt reaches the model as you wrote it, a LoRA's trigger word in front.
        An item that does not fit its preset (an unknown variant, a size past the
        limits, a LoRA id that is none) fails with the reason in its line. Which preset
        fits which look, and how each takes a prompt, is in the guide. The size is
        width and height. Draw only what was asked for: no props or story nobody named.

        Example: items=[{"prompt": "..."}], count=2. Returns: the batch id and each
        slot (its image's short id once made)."""
        from .places import place_of
        chat = await ctx.store.read(lambda c: place_of(c, ctx.conversation_id)) == "chat"
        return await batch_generate(self.app, ctx, "images", items, count, wait,
                                    lambda b, s: self._text(b, s, chat), clip=True)

    @tool()
    async def read(
        self,
        ctx: ToolContext,
        batch: Annotated[str, "The batch id from generate, like 3f2a9c1e."],
    ) -> ToolResult:
        """A batch's state: each slot, waiting, being made, made (with its image's
        short id), failed (with why) or stopped. Returns: one line per image."""
        return await batch_read(ctx, "images", batch, self._text)

    @tool(read_only=False, destructive=False)
    async def stop(
        self,
        ctx: ToolContext,
        batch: Annotated[str, "The batch id from generate, like 3f2a9c1e."],
    ) -> ToolResult:
        """Stop a batch: the image being made is interrupted, the ones after it are not
        made. What was made stays. Returns: the batch."""
        return await batch_stop(self.app, ctx, "images", batch, self._text)

    @tool()
    async def list_presets(
        self,
        ctx: ToolContext,
        kind: Annotated[Literal["preset", "lora", "model"], "preset: the presets. lora "
                        "or model: what one preset can use, from the ComfyUI server "
                        "itself."] = "preset",
        preset: Annotated[str | None, "For lora and model: a preset id. Empty: the "
                                      "first preset for new images that has them."] = None,
        category: Annotated[str | None, "LoRAs: character, style, realism, nsfw, "
                                        "anatomy, concept, tool. Models: base, finetune, "
                                        "edit. Empty: all."] = None,
        search: Annotated[str | None, "Words that must all appear in the id, name, tags "
                                      "or triggers."] = None,
    ) -> ToolResult:
        """The presets, or the models and LoRAs one preset can use: id, category, name,
        what it does, trigger words, default strength. Read it before you give an item
        a model or LoRAs. Returns: one line each, grouped by category."""
        from .. import image_catalog
        from ..settings.registry import provider_headers
        found = self._presets()
        if kind == "preset":
            return ToolResult(content="\n".join(
                f"- `{p.id}` {p.label} ({p.family}), "
                f"{'edits' if p.task == 'edit' else 'new images'}: {p.good_at}"
                for p in found) or "No preset is set.", display={"count": len(found)})
        with_catalog = [p for p in found if p.models or p.loras]
        want = (preset or "").strip().lower()
        p = next((x for x in with_catalog if want in (x.id, x.label.lower())), None) \
            if want else next((x for x in with_catalog if x.task == "generate"),
                              with_catalog[0] if with_catalog else None)
        if p is None:
            names = ", ".join(x.id for x in with_catalog) or "none"
            return ToolResult(content=f"No preset with models and LoRAs matches. Those "
                                      f"presets are: {names}.", is_error=True)
        s = self.cfg.settings
        pid = next(s.split(r)[0] for r in s.chain(presets_mod.ROLES["images"])
                   if s.split(r)[1] == p.id)
        try:
            cat = await image_catalog.load(
                s.providers[pid]["base_url"], provider_headers(s, pid),
                transport=self.app.llm.router.transport if self.app else None)
        except Exception as e:
            return ToolResult(content=f"ComfyUI did not list its models "
                                      f"({type(e).__name__}).", is_error=True)
        prefixes = p.models if kind == "model" else p.loras
        rows = cat.list(kind, prefixes, category=category, search=search)
        if not rows:
            return ToolResult(content=f"No {kind} of {p.label} matches.")
        lines, last = [], None
        for r in rows[:40]:
            if r.category != last:
                last = r.category
                lines.append(f"\n{last}:")
            lines.append(f"- {r.line()}")
        text = f"{kind}s of {p.label} (`{p.id}`):" + "\n".join(lines)
        if len(rows) > 40:
            text += f"\n\n{len(rows) - 40} more. Narrow with category or search."
        return ToolResult(content=text.strip(), display={"count": len(rows)})

    @tool()
    async def list_images(self, ctx: ToolContext) -> ToolResult:
        """The images in this conversation, newest first: the ones you made, each with
        the prompt, the preset, the size and the seed it was made from, and the ones
        {{username}} attached. Use it to see what an earlier image was asked for before
        you edit it or make a variation. Returns: one block per image with its short
        id, the source of an edit."""
        rows = await attachments.images(ctx.store, ctx.conversation_id)
        if not rows:
            return ToolResult(content="There are no images in this conversation.")
        blocks = []
        for r in rows[:30]:
            when = r["created_at"][:16].replace("T", " ")
            made = r["made"]
            if made:
                att = made.get("attachment") or {}
                head = (f"{short(r['id'])}  {when}  you made it with {made.get('preset')}, "
                        f"{att.get('width')}x{att.get('height')}, seed {made.get('seed')}")
                if made.get("source"):
                    head += f", edited from {_sources(made)}"
                if made.get("model"):
                    head += f", model {made['model']}"
                if made.get("loras"):
                    head += f", LoRAs {', '.join(made['loras'])}"
                lines = [head, f"  prompt: {made.get('prompt') or '(not recorded)'}"]
                if made.get("negative"):
                    lines.append(f"  negative: {made['negative']}")
                blocks.append("\n".join(lines))
            elif r["role"] == "user":
                blocks.append(f"{short(r['id'])}  {when}  {self.cfg.user} attached "
                              f"{r['filename']}")
            else:
                blocks.append(f"{short(r['id'])}  {when}  you made {r['filename']} (what "
                              f"it was asked for is not recorded)")
        more = len(rows) - 30
        text = "\n\n".join(blocks)
        if more > 0:
            text += f"\n\n{more} older images are not listed."
        return ToolResult(content=text, display={"count": len(rows)})


def _with_presets(ids: tuple[str, ...]):
    """`generate` as a function of its own whose items' `preset` is an enum of `ids`,
    the live presets: the schema it is offered names them, so an invented one is
    refused before the batch."""
    # note: a copy of the function per skill built, rebuilt with the skills on reload
    fn = ImageGenerator.generate
    hints = typing.get_type_hints(Item, include_extras=True)
    about = typing.get_args(hints["preset"])[1:]
    item = TypedDict("Item", {**hints, "preset": Annotated[Literal[ids], *about]},
                     total=False)
    out = types.FunctionType(fn.__code__, fn.__globals__, fn.__name__, fn.__defaults__,
                             fn.__closure__)
    out.__kwdefaults__, out.__doc__ = fn.__kwdefaults__, fn.__doc__
    out.__dict__.update(fn.__dict__)
    of_fn = typing.get_type_hints(fn, include_extras=True)
    out.__annotations__ = {**of_fn, "items": Annotated[list[item],
                                                       *typing.get_args(of_fn["items"])[1:]]}
    return out
