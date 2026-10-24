"""Impersonate: the chat model writes the user's next message, in their voice, from the
conversation so far: their own last messages are the sample of how they write, and
the message is about as long as theirs (at most half again as long). What the user
already typed is where it starts: the model writes on from it. One request, thinking
and voice off, nothing stored; the text goes back to the composer for the user to edit
or send."""
from __future__ import annotations

from ..backends.base import Done, TextDelta
from ..turns import tokens
from . import tree

PROMPT = ("Write {{username}}'s next message in this conversation, as {{username}} writes "
          "it, not as {name}. One message, nothing else.")
SAMPLE = "How {{username}} writes, their last messages:\n\n{lines}"
LENGTH = ("Their messages are about {words} words long. Write about as many, never more "
          "than {most}.")
SAMPLES = 3               # the user's last messages that show how they write
# a model that cannot be prefilled is told where the message starts
STARTS = "The message starts with these words; write it on from there:\n\n{start}"
MAX_CHARS = 4000          # what one message of theirs may be; the rest is cut




def _joined(start: str, more: str) -> str:
    """The typed start and the model's words as one message: a space between them
    unless one side already has it or the words go on with punctuation."""
    if not start or not more or start[-1].isspace() or more[0].isspace() \
            or more[0] in ".,;:!?)'\"":
        return start + more
    return f"{start} {more}"


async def write(store, cfg, backend, cid: str, start: str = "") -> str:
    """The user's next message, beginning with `start` (what they typed). The active
    path goes as one transcript ("name: text"), newest messages first into the room
    the model's window leaves. With prefill the request ends with `start` as the
    message being written; otherwise the instruction quotes it. KeyError: no such
    conversation."""
    from ..turns.prompt import supports_prefill
    chain = await store.read(lambda c: tree.path(c, cid))
    room = tokens.window(backend) // 2
    lines: list[str] = []
    theirs: list[str] = []        # the user's own messages, newest first
    for m in reversed(chain):
        text = (m["content"] or "").strip()
        if m["role"] not in ("user", "assistant") or not text:
            continue
        mine = m["role"] == "user"
        if mine and m["author"] == "user":
            theirs.append(text)
        who = cfg.user if mine else cfg.name
        line = f"{who}: {text}"
        room -= tokens.estimate(line)
        if room < 0:
            break
        lines.append(line)
    start = start.strip()
    prefill = bool(start) and supports_prefill(backend)
    system = cfg.substitute(PROMPT.replace("{name}", cfg.name))
    sample = theirs[:SAMPLES]
    most = MAX_CHARS
    if sample:
        words = round(sum(len(t.split()) for t in sample) / len(sample)) or 1
        system += "\n\n" + LENGTH.format(words=words, most=-(-words * 3 // 2))
        system += "\n\n" + cfg.substitute(SAMPLE).replace(
            "{lines}", "\n\n".join(reversed(sample)))
        most = max(len(start) + 20, sum(len(t) for t in sample) * 3 // (2 * len(sample)))
    if start and not prefill:
        system += "\n\n" + STARTS.format(start=start)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": "\n\n".join(reversed(lines))}]
    if prefill:
        messages.append({"role": "assistant", "content": start})
    parts: list[str] = []
    events = backend.stream(messages, [])
    try:
        async for ev in events:
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
                if sum(len(p) for p in parts) > min(most, MAX_CHARS):
                    break
            elif isinstance(ev, Done):
                break
    finally:                        # frees a queued backend's model slot right away
        if (aclose := getattr(events, "aclose", None)) is not None:
            await aclose()
    text = "".join(parts)
    label = f"{cfg.user}:"          # a model that echoes the transcript's label
    if text.lstrip().startswith(label):
        text = text.lstrip()[len(label):].lstrip()
    if not prefill:
        text = text.strip()
        if start and text.startswith(start):    # a model that wrote the start out again
            text = text[len(start):]
    return _cut(_joined(start, text.rstrip()), min(most, MAX_CHARS))


def _cut(text: str, most: int) -> str:
    """At most `most` characters, ended where a sentence or at least a word ends."""
    text = text.strip()
    if len(text) <= most:
        return text
    head = text[:most]
    end = max(head.rfind(x) for x in (". ", "! ", "? ", ".\n", "!\n", "?\n"))
    return (head[:end + 1] if end > most // 2 else head.rsplit(" ", 1)[0]).strip()
