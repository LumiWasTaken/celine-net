"""One question about one image, answered by a model of a vision role: Screen reading
(role screen) for browser__look (skills/browser.py), Photo descriptions (role vision)
for images.

The instruction text (vision.md) has the model describe what is visible, then ends
with the form of the answer: an id, a box or a few sentences.
An answer that is not in that form is none; nothing is asked twice.

A box is on 0 to 1000 of the image sent, in Qwen-VL's bbox_2d form: the Qwen-VL family
and MiniCPM-V write boxes on that scale as text.

The request waits in the LLM queue at the priority of the turn it serves, thinking
off, and takes the role's first model alone (no fallback): when no model of the role
can answer now, or its server refuses, `ask` raises Unavailable at once instead of
waiting for one (the queue's fallback loop waits for a model without end).
"""
from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass

from PIL import Image

from . import prompts
from .backends.base import BackendError, Done, TextDelta
from .backends.queue import Priority
from .settings.thinking import OFF

# what the model is told: vision.md beside this module
# the last line of the request, by the form of the answer
FORM = {
    "id": "Each element has a pink box with its id in the box's top-right corner, like e14. "
          "Answer with the id only, or none.",
    "box": "Locate what the question asks about and output its bounding box as JSON: "
           "{\"bbox_2d\": [x1, y1, x2, y2]}, with coordinates normalized to 0-1000. Output "
           "the JSON only, or none.",
    "text": "Answer in at most 5 short sentences. If asked whether something is there, say "
            "where it is, or none.",
}
TOKENS = {"id": 20, "box": 60, "text": 200}    # the most an answer may take
ID = re.compile(r"e?(\d+)", re.I)
# where a box begins: {"bbox_2d": [...]} (code fences and words around it are fine),
# <box>(x1,y1),(x2,y2)</box>, Qwen's own <|box_start|>(x1,y1),(x2,y2)<|box_end|>, or a
# bare [x1, y1, x2, y2]; its four numbers follow
BOX = re.compile(r"bbox_2d|<box>|<\|box_start\|>|(?=\[\s*-?\d)")
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def box(said: str) -> tuple[int, int, int, int] | None:
    """The first box in a model's answer, (x1, y1, x2, y2) on 0 to 1000, left before
    right and top before bottom; None without one, with a number below 0 or past 1000
    (pixels, not the scale asked for), or on 0 to 1 (every number at most 1, one with a
    decimal point: rounded, it would be the top-left corner)."""
    m = BOX.search(said)
    raw = NUMBER.findall(said, m.end())[:4] if m else []
    f = [float(n) for n in raw]
    if len(f) < 4 or min(f) < 0 or max(f) > 1000 or (max(f) <= 1 and any("." in n for n in raw)):
        return None
    v = [round(n) for n in f]
    return min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3])


class Unavailable(Exception):
    """No model of the role answered; the message says why."""


@dataclass
class Answer:
    value: str | None   # the id (e14), the box ("x1,y1,x2,y2" on 0 to 1000) or the text; None: none
    said: str           # the model's own words, as it said them
    model: str          # the model that answered


def media_type(image: bytes) -> str:
    """The image's type by its bytes (Pillow); image/png for bytes Pillow does not know."""
    try:
        with Image.open(io.BytesIO(image)) as im:
            return Image.MIME.get(im.format or "", "image/png")
    except Exception:            # a broken file: Pillow raises many kinds
        return "image/png"


def messages(image: bytes, question: str, answer_as: str) -> list[dict]:
    text = f"{prompts.text(__file__, 'vision.md').rstrip()}\n\nQuestion: {question}\n{FORM[answer_as]}"
    url = f"data:{media_type(image)};base64,{base64.b64encode(image).decode()}"
    return [{"role": "user", "content": [{"type": "text", "text": text},
                                         {"type": "image_url", "image_url": {"url": url}}]}]


async def ask(llm, image: bytes, question: str, answer_as: str, *, conversation_id: str,
              turn_id: str, role: str = "screen") -> Answer:
    """The answer to `question` about the image, as `answer_as` (id, box or text), from
    the app's LLM layer (`llm`, None when it is not running), by a model of `role`."""
    if llm is None:
        raise Unavailable("the app's model layer is not running.")
    ordered = await llm.router.order(role)
    if not ordered.refs:
        raise Unavailable(ordered.sentence())
    qb = llm.bind(None, role=role,
                  priority=llm.turn_priority.get(turn_id, Priority.AGENT),
                  conversation_id=conversation_id, turn_id=turn_id,
                  body={"temperature": 0, "max_tokens": TOKENS[answer_as]}, thinking=OFF,
                  fallback=False)
    parts: list[str] = []
    try:
        events = qb.stream(messages(image, question, answer_as), [])
        try:
            async for ev in events:
                if isinstance(ev, TextDelta):
                    parts.append(ev.text)
                elif isinstance(ev, Done):
                    break
        finally:
            await events.aclose()
    except BackendError as e:
        raise Unavailable(str(e)) from None
    raw = "".join(parts)
    word = raw.strip().strip(" .*\"'`")       # read for the id; she gets raw
    if answer_as == "text":
        value = raw if raw.strip() else None        # the words as the model said them
    elif answer_as == "box":
        value = ",".join(map(str, b)) if (b := box(raw)) else None
    else:
        m = ID.fullmatch(word)
        value = "e" + m[1] if m else None
    return Answer(value, raw, getattr(qb.answered, "model", ""))


async def describe(llm, data: bytes, conversation_id: str, turn_id: str,
                   question: str | None = None) -> str:
    """An image in words, by the vision role (the shell's and the email's images)."""
    try:
        got = await ask(llm, data, str(question or "") or
                        "Describe this image, and quote any text in it.",
                        "text", conversation_id=conversation_id, turn_id=turn_id,
                        role="vision")
    except Unavailable as e:
        return f"No description: {e}"
    return f"Description by {got.model}: {got.value}" if got.value else \
        "No description: the model did not answer."
