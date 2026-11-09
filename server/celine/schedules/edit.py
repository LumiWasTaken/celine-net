"""A Routine written or run, by the user (schedules/api.py) or by it (schedules/skill.py):
one path, the same checks. A write goes to var/settings/schedules/<name>.toml (and its
reviewer prompt beside it), reloads the config in place, records its texts in their
History by who wrote them and wakes the scheduler."""
from __future__ import annotations

import re

from ..settings import files, seed
from ..settings.files import Invalid
from . import NAME, check_after, parse
from ..core.event_types import SETTINGS_CHANGED

CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
FIELDS = ("label", "summary", "cron", "time", "prompt", "after", "reviewer", "reviewer_prompt",
          "turns", "note", "role", "on", "idle_only", "wall_minutes",
          "daytime", "on_fail")
KEPT = ("daytime", "on_fail")   # left out of a write, the stored values stay


def record_texts(c, s, by: str) -> None:
    """Its prompt and its reviewer prompt into their History (texts.py):
    `routine:<name>:prompt` and `routine:<name>:reviewer`, the keys of the editor's
    page, written by `by` (user or it)."""
    from ..texts import record
    record(c, f"routine:{s.name}:prompt", s.prompt, by)
    if s.reviewer_prompt:
        record(c, f"routine:{s.name}:reviewer", s.reviewer_prompt, by)


async def save(app, name: str, body: dict, by: str):
    """{label, cron or time, prompt, ...} (FIELDS): the whole schedule, made or replaced.
    Raises Invalid with the field and one sentence. -> (the Schedule, applied)."""
    from .. import reload
    from ..settings import write
    if not NAME.match(name):
        raise Invalid("name", "A name is lowercase letters, digits, '-' or '_', starting "
                              "with a letter.")
    body = dict(body)
    for k in ("label", "prompt"):
        if k not in body:
            raise Invalid(k, f"'{k}' is missing.")
    for k in body:
        if k not in FIELDS:
            raise Invalid(k, f"'{k}' is not a schedule setting.")
    if "cron" in body and "time" in body:
        raise Invalid("time", "Give the time of day or a cron line, not both.")
    if "cron" not in body and "time" not in body:
        raise Invalid("time", "Send the time of day or a cron line.")
    if "time" in body:
        m = CLOCK.match(str(body.pop("time") or ""))
        if not m:
            raise Invalid("time", "The time is HH:MM, such as 02:00.")
        body["cron"] = f"{int(m[2])} {int(m[1])} * * *"
    if body.get("wall_minutes") is None:
        body.pop("wall_minutes", None)
    text = body.pop("reviewer_prompt", "") or ""
    if not isinstance(text, str):
        raise Invalid("reviewer_prompt", "The reviewer prompt is text.")
    body["reviewer"] = (body.get("reviewer") or f"{name}-reviewer.md") if text.strip() else ""
    every = app.cfg.schedules
    if (old := every.get(name)) is not None:
        body = {**{k: getattr(old, k) for k in KEPT}, **body}
    s = parse(name, body, text if body["reviewer"] else None)
    check_after({k: v for k, v in every.items() if k != name}, s)
    folder = seed.schedules_dir(files.settings_dir(app.cfg))
    async with write.lock():
        files.write_toml(folder / f"{name}.toml", s.raw())
        if s.reviewer:
            files.write_bytes(folder / s.reviewer, (s.reviewer_prompt + "\n").encode())
        applied = reload.apply(app)
    await app.store.write(lambda c: record_texts(c, s, by))
    app.store.emit_global({"type": SETTINGS_CHANGED, "what": ["schedules"]})
    if app.schedules is not None:
        app.schedules.wake()
    return app.cfg.schedules.get(name, s), applied
