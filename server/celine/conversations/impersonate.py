"""Impersonate: the chat model writes the user's next message, in their voice, from the
conversation so far: their own last messages are the sample of how they write. What
the user already typed is where it starts. One request, thinking and voice off,
nothing stored; the text goes back to the composer as the model wrote it, for the user
to edit or send."""
from __future__ import annotations

from ..backends.base import Done, TextDelta
from ..turns import tokens
from . import tree

PROMPT = ("Write {{username}}'s next message in this conversation, as {{username}} writes "
          "it, not as {name}. One message, nothing else.")
SAMPLE = "How {{username}} writes, their last messages:\n\n{lines}"
SAMPLES = 3               # the user's last messages that show how they write
# a model that cannot be prefilled writes the whole message, from these words on
STARTS = "The message starts with these words; write the whole message:\n\n{start}"


async def write(store, cfg, backend, cid: str, start: str = "") -> str:
    """The user's next message, beginning with `start` (what they typed). The active
    path goes as one transcript ("name: text"), newest messages first into the room
    the model's window leaves. With prefill the request ends with `start` as the
    message being written, and the text is `start` and what the model wrote on;
    otherwise the instruction quotes it, and the text is what the model wrote. Nothing
    is changed in it. KeyError: no such conversation."""
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
    if sample:
        system += "\n\n" + cfg.substitute(SAMPLE).replace(
            "{lines}", "\n\n".join(reversed(sample)))
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
            elif isinstance(ev, Done):
                break
    finally:                        # frees a queued backend's model slot right away
        if (aclose := getattr(events, "aclose", None)) is not None:
            await aclose()
    text = "".join(parts)
    return start + text if prefill else text
