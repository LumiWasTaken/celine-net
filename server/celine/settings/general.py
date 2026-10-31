"""General settings: the everyday choices that are not about models or tools, and
the Advanced ones (time limits, sizes, how much is kept), each defined once here.

Your name lives here (its own name and pronouns are its own, celine.her); how the app is
served (host, port, login) stays in config/celine.toml. Each field here has a schema the
UI renders as a generic form; an `advanced` one is on the Advanced page. Every reader
asks `cfg.r(key)` (or `value`) when it runs, so a save applies at once.

A skill's own numbers are not here: each skill declares them beside its code (`KNOBS`,
skills.knob), and `fields()` adds those of every skill that is there, keyed
`<skill>.<name>` and grouped under the skill's label. var/settings/general.toml keeps them
in a table per skill; a table of a skill that is gone is dropped with it.
"""
from __future__ import annotations

import functools
from typing import Any

from .files import Invalid
from .form import clean

FIELDS: list[dict[str, Any]] = [
    {"key": "user_name", "label": "Your name", "type": "text",
     "help": "What the agent calls you.", "default": "User", "required": True},
    {"key": "timezone", "label": "Time zone", "type": "text",
     "help": "Your time zone, such as Europe/Berlin or America/New_York.", "default": "UTC", "required": True,
     "zone": True},
    {"key": "user_bio", "label": "About you", "type": "text", "default": "", "max_len": 2000},
    {"key": "smart_search_refresh_minutes", "label": "Update the search index every",
     "type": "number", "unit": "minutes", "min": 0, "max": 1440,
     "help": "0 turns the updates off.", "default": 15},
    {"key": "lock_archived", "label": "Lock archived chats", "type": "bool",
     "help": "An archived chat stays as it is until you write in it again.", "default": True},
    {"key": "image_captions_auto", "label": "Describe new photos in Messages",
     "type": "bool",
     "help": "Each new photo gets a description, so Search finds it by what it shows.",
     "default": False},
    # turns/prompt.py: on, her prompt carries turns/ask_before_setup.md
    {"key": "ask_before_setup", "label": "Ask before setting up services", "type": "bool",
     "help": "Before the agent sets up a server, a model or a service, it explains "
             "the plan and waits for your OK.", "default": True},
    {"key": "code_websearch", "label": "Web search for the coding agent", "type": "bool",
     "help": "The coding agent searches the web through Exa, an outside service.", "default": False},
    # server/celine/guard: read at every call, so a change applies at once; off is the
    # Guard feature switch (Settings, Features)
    {"key": "guard", "label": "Guard mode", "type": "choice",
     "choices": [["watch", "Watch"], ["on", "On"]],
     "help": "Watch: every step runs at once, and Bastet writes down in Events what she "
             "would have done. On: Bastet checks a risky step before it runs.",
     "default": "watch"},
    # Advanced: whole numbers, read where they are used (turns, jobs, uploads, backups)
    *({"key": k, "label": label, "type": "number", "unit": unit, "min": lo, "max": hi,
       "default": d, "advanced": True, **({"help": tip} if tip else {})}
      for k, label, unit, lo, hi, d, tip in (
        ("max_concurrent_turns", "Turns at once", None, 1, 64, 6,
         "How many chats may generate at the same time."),
        ("max_tool_iterations", "Tool rounds per turn", None, 0, 100_000, 0,
         "0 means no limit; only the turn time limit applies."),
        ("turn_wall_clock_seconds", "Turn time limit", "seconds", 1, 86_400, 1800,
         "The agent's work time in one turn, tools included."),
        ("last_step_seconds", "Last answer time", "seconds", 1, 3600, 180,
         "After the turn time limit, the step that is running gets this long to finish."),
        ("tool_timeout_seconds", "Tool time limit", "seconds", 1, 86_400, 180, None),
        ("routine_max_minutes", "Routine time limit", "minutes", 0, 100_000, 180,
         "The longest one routine run may take. 0 means no limit."),
        ("mission_max_hours", "Mission time limit", "hours", 0, 100_000, 0,
         "The longest one mission may take, from its start. 0 means no limit."),
        ("mission_max_sub_agents", "Sub-agents per mission", None, 0, 10_000, 0,
         "0 means no limit."),
        ("job_timeout_seconds", "Job time limit", "seconds", 1, 604_800, 3600,
         "A tool that runs in the background is stopped after this."),
        ("embed_query_timeout_seconds", "Search embedding time limit", "seconds", 1,
         3600, 60, "A search waits this long for its embedding model."),
        ("busy_retry_seconds", "Busy model wait", "seconds", 1, 86_400, 10,
         "Background work asks a busy model again after this, twice as long each time."),
        ("busy_retry_max_seconds", "Longest busy model wait", "seconds", 1, 86_400, 600,
         None),
        ("history_messages", "Messages per request", "messages", 0, 100_000, 200,
         "The most messages one request carries. 0 means no limit."),
        ("events_keep", "Log entries per chat", "entries", 0, 1_000_000, 500,
         "How many log entries a chat keeps after its turn ends."),
        ("backup_keep_days", "Backups kept", "days", 1, 3650, 7, None),
        ("max_attachments", "Attachments per message", None, 0, 1000, 10, None),
        ("max_upload_mb", "Largest upload", "MB", 1, 1_000_000, 512,
         "The largest file you can add to Documents."),
        ("max_attachment_bytes", "Largest attachment", "bytes", 1, 10_000_000_000,
         20_000_000, None),
        ("inline_text_max_bytes", "Largest text read inline", "bytes", 0, 100_000_000,
         200_000, "A larger text file is attached, not put into the message."),
        ("max_message_bytes", "Largest message", "bytes", 1, 100_000_000, 1_000_000, None),
        ("requests_keep", "Model requests kept", "days", 0, 36_500, 0,
         "How long to keep what each step sent to its model. 0 keeps it forever."),
        # kb/documents_extract.py: what one document may cost to read
        ("document_part_mb", "Largest part in a document", "MB", 1, 100_000, 256,
         "An EPUB or Word file with a larger part unpacked is not read whole."),
        ("document_html_mb", "Largest web page document", "MB", 1, 100_000, 64, None),
        ("document_pdf_pages", "PDF pages read", "pages", 1, 1_000_000, 5000, None),
        ("document_pdf_seconds", "PDF read time limit", "seconds", 1, 86_400, 600, None),
    )),
    # Advanced switches, read where they are used (schedules, missions, answers)
    *({"key": k, "label": label, "type": "bool", "default": d, "advanced": True,
       "help": tip}
      for k, label, d, tip in (
        ("routines_wait_for_missions", "Routines wait for missions", False,
         "A routine waits until no mission is running."),
        ("missions_resume_after_unfreeze", "Continue missions after Unfreeze", True,
         "A mission that a freeze stopped continues automatically after Unfreeze."),
        # turns/reply.py: the [[kb:id]] citations of an answer
        ("verify_citations", "Check citations", True,
         "Every message or document an answer cites must be one a tool showed."),
    )),
]


@functools.cache
def schema() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], frozenset[str]]:
    """(every field: FIELDS, then each skill's knobs, advanced and under the skill's
    label; the same by key; the skills that are there). Read once: a skill comes or goes
    with the code, never while it runs."""
    from ..skills import BUILTIN, knob_fields, module
    from .tools_meta import SKILLS, humanize
    out = [*FIELDS, *({**f, "advanced": True,
                       "group": SKILLS.get(name, {}).get("label") or humanize(name)}
                      for name, f in knob_fields())]
    there = frozenset(name for name, mod, _ in BUILTIN if module(mod) is not None)
    return out, {f["key"]: f for f in out}, there


def fields() -> list[dict[str, Any]]:
    return schema()[0]


def value(general: dict[str, Any], key: str) -> Any:
    """A general setting, or its default when it was never saved."""
    return general.get(key, schema()[1][key]["default"])


def validate(data: Any) -> dict[str, Any]:
    """Flat keys in, flat keys out; a skill's table (`[web]` in the file) is read as its
    `web.<name>` keys."""
    if not isinstance(data, dict):
        raise Invalid("general", "General settings are an object.")
    _, by_key, there = schema()
    flat: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            flat.update({f"{k}.{name}": x for name, x in v.items()})
        else:
            flat[k] = v
    out: dict[str, Any] = {}
    for k, v in flat.items():
        f = by_key.get(k)
        path = f"general.{k}"
        if f is None and "." in k and k.partition(".")[0] not in there:
            continue                # the knob of a skill that is gone: gone with it
        if f is None:
            raise Invalid(path, f"'{k}' is not a general setting.")
        v = clean(path, f, v)
        if v is not None:
            out[k] = v
    if value(out, "job_timeout_seconds") <= value(out, "tool_timeout_seconds"):
        raise Invalid("general.job_timeout_seconds", "The job time limit must be longer "
                                                      "than the tool time limit.")
    if value(out, "busy_retry_max_seconds") < value(out, "busy_retry_seconds"):
        raise Invalid("general.busy_retry_max_seconds", "The longest busy model wait "
                                                         "must be at least the first.")
    return out
