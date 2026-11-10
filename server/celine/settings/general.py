"""General settings: the everyday choices that are not about models or tools, and
the Advanced ones (time limits, sizes, how much is kept), each defined once here.

Your name lives here (its own name and pronouns are its own, celine.agent); how the app is
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

# the Advanced page's groups in their order; a skill's knobs follow, each under its label
GROUPS = ("Turns", "Models", "Routines and missions", "Files", "Storage")


def _number(group: str, k: str, label: str, unit: str | None, lo: float, hi: float, d: float,
            tip: str, zero: str | None = None) -> dict[str, Any]:
    """`zero`: what the field shows for 0 ("No limit")."""
    return {"key": k, "label": label, "type": "number", "unit": unit, "min": lo, "max": hi,
            "default": d, "advanced": True, "group": group, "help": tip,
            **({"zero": zero} if zero else {})}


def _switch(group: str, k: str, label: str, d: bool, tip: str) -> dict[str, Any]:
    return {"key": k, "label": label, "type": "bool", "default": d, "advanced": True,
            "group": group, "help": tip}


FIELDS: list[dict[str, Any]] = [
    {"key": "user_name", "label": "Your name", "type": "text", "default": "User",
     "required": True},
    {"key": "timezone", "label": "Time zone", "type": "text",
     "help": "Routines run and dates show in this zone, written like Europe/Berlin or "
             "America/New_York.", "default": "UTC", "required": True, "zone": True},
    {"key": "user_bio", "label": "About you", "type": "text", "default": "", "max_len": 2000,
     "help": "{{name}} reads this in every chat."},
    {"key": "smart_search_refresh_minutes", "label": "Update the search index every",
     "type": "number", "unit": "minutes", "min": 0, "max": 1440,
     "help": "How often new messages are added to Search. 0 turns the updates off.",
     "default": 15},
    {"key": "lock_archived", "label": "Lock archived chats", "type": "bool",
     "help": "An archived chat takes no messages but yours, and its running work and missions "
             "stop.", "default": True},
    {"key": "image_captions_auto", "label": "Describe new photos in Messages",
     "type": "bool",
     "help": "Each new photo gets a description, so Search finds it by what it shows.",
     "default": False},
    # turns/prompt.py: on, its prompt carries turns/ask_before_setup.md
    {"key": "ask_before_setup", "label": "Ask before setting up services", "type": "bool",
     "help": "A server, a model or a service is set up only after the plan is explained "
             "and you say OK.", "default": True},
    # settings/agent_models.py picked_by_user: on, a role list makes no cloud model the user's pick
    {"key": "ask_listed_cloud", "label": "Ask before cloud models from your lists",
     "type": "bool",
     "help": "A cloud model from your role lists asks you too before a sub-agent, mission "
             "or coding session runs on it. Off, only cloud models outside your lists ask.",
     "default": False},
    {"key": "code_websearch", "label": "Web search for the coding agent", "type": "bool",
     "help": "The coding agent searches the web through Exa, an outside service.", "default": False},
    # server/celine/guard: read at every call, so a change applies at once; off is the
    # Guard feature switch (Settings, Features). Its "?" is the Mode section's on Settings,
    # Guard (guard/view.py PAGE_HELP)
    {"key": "guard", "label": "Guard mode", "type": "choice",
     "choices": [["watch", "Warn"], ["on", "Enforce"]], "default": "watch"},
    # Advanced, one group after another: read where they are used (turns, jobs, uploads,
    # backups, schedules, missions, answers)
    _number("Turns", "max_concurrent_turns", "Turns at once", None, 1, 64, 6,
            "How many chats, routines and missions may generate at the same time."),
    _number("Turns", "max_tool_iterations", "Tool rounds per turn", None, 0, 100_000, 0,
            "The most rounds of tool calls in one turn. 0 means no limit, so only the turn "
            "time limit applies.", zero="No limit"),
    _number("Turns", "turn_wall_clock_seconds", "Turn time limit", "seconds", 1, 86_400, 1800,
            "The longest one turn may work, tools included."),
    _number("Turns", "last_step_seconds", "Grace period", "seconds", 1, 3600, 180,
            "After the turn time limit, the tool round under way gets this long to finish."),
    _number("Turns", "tool_timeout_seconds", "Tool time limit", "seconds", 1, 86_400, 180,
            "The longest one tool call may take before it fails."),
    _number("Turns", "job_timeout_seconds", "Job time limit", "seconds", 1, 604_800, 3600,
            "A tool that runs in the background is stopped after this."),
    # turns/reply.py: the [[kb:id]] citations of an answer
    _switch("Turns", "verify_citations", "Check citations", True,
            "Every message or document a reply cites must be one a tool showed."),
    # turns/warm.py
    _number("Models", "keep_warm_minutes", "Keep cache warm", "minutes", 0, 1440, 0,
            "After the newest chat's last reply, the app resends its request every 4 minutes for "
            "this long, so the model stays loaded and its prompt cache stays warm.", zero="Off"),
    _number("Models", "embed_query_timeout_seconds", "Search embedding time limit", "seconds",
            1, 3600, 60, "How long a search waits for the Embeddings model."),
    _number("Models", "busy_retry_seconds", "Busy model retry delay", "seconds", 1, 86_400, 10,
            "Background work retries a busy model after this delay, doubling it after each "
            "retry."),
    _number("Models", "busy_retry_max_seconds", "Busy model max retry delay", "seconds", 1,
            86_400, 600, "The longest delay between two retries of a busy model."),
    _number("Routines and missions", "routine_max_minutes", "Routine time limit", "minutes",
            0, 100_000, 180, "The longest one routine run may take. 0 means no limit.", zero="No limit"),
    _switch("Routines and missions", "routines_wait_for_missions",
            "Routines wait for missions", False,
            "A routine starts only when no mission is running."),
    _number("Routines and missions", "mission_max_hours", "Mission time limit", "hours", 0,
            100_000, 0, "The longest one mission may take, from its start. 0 means no limit.", zero="No limit"),
    _number("Routines and missions", "mission_max_sub_agents", "Sub-agents per mission", None,
            0, 10_000, 0, "The most sub-agents one mission may start. 0 means no limit.", zero="No limit"),
    _switch("Routines and missions", "missions_resume_after_unfreeze",
            "Continue missions after Unfreeze", True,
            "A mission that a freeze stopped continues by itself after Unfreeze."),
    _number("Files", "max_attachments", "Attachments per message", None, 0, 1000, 10,
            "The most files one message may carry."),
    _number("Files", "max_upload_mb", "Largest upload", "MB", 1, 1_000_000, 512,
            "The largest file you can add to Documents."),
    _number("Files", "max_attachment_bytes", "Largest attachment", "bytes", 1,
            10_000_000_000, 20_000_000, "The largest file one message may carry."),
    _number("Files", "inline_text_max_bytes", "Largest text read inline", "bytes", 0,
            100_000_000, 200_000, "A larger text file is attached, not put into the message."),
    _number("Files", "max_message_bytes", "Largest message", "bytes", 1, 100_000_000,
            1_000_000, "The longest text one message may hold."),
    # kb/documents_extract.py: what one document may cost to read
    _number("Files", "document_part_mb", "Largest file inside an EPUB or Word file", "MB", 1,
            100_000, 256, "An EPUB or Word file with a larger file inside is not read whole."),
    _number("Files", "document_html_mb", "Largest HTML document", "MB", 1, 100_000, 64,
            "A larger saved web page in Documents is not read."),
    _number("Files", "document_pdf_pages", "PDF pages read", "pages", 1, 1_000_000, 5000,
            "The most pages of one PDF that are read."),
    _number("Files", "document_pdf_seconds", "PDF read time limit", "seconds", 1, 86_400, 600,
            "The longest reading one PDF may take."),
    _number("Storage", "events_keep", "Stream events per chat", "entries", 0, 1_000_000, 500,
            "How many live-update events an idle chat keeps; a page further behind reloads "
            "the chat."),
    _number("Storage", "backup_keep_days", "Backups kept", "days", 1, 3650, 7,
            "How long a backup is kept before it is deleted."),
    _number("Storage", "requests_keep", "Model requests kept", "days", 0, 36_500, 30,
            "How long to keep what each step sent to its model; its token counts and "
            "times stay. 0 keeps it forever."),
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
        raise Invalid("general.busy_retry_max_seconds", "Busy model max retry delay "
                                                         "must be at least Busy model retry delay.")
    return out
