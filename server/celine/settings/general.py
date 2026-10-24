"""General settings: the everyday choices that are not about models or tools, and
the Advanced ones (time limits, sizes, how much is kept), each defined once here.

Your name lives here (its own name and pronouns are its own, celine.her); how the app is
served (host, port, login) stays in config/celine.toml. Each field here has a schema the
UI renders as a generic form; an `advanced` one is on the Advanced page. Every reader
asks `cfg.r(key)` (or `value`) when it runs, so a save applies at once.
"""
from __future__ import annotations

from typing import Any

from .files import Invalid

FIELDS: list[dict[str, Any]] = [
    {"key": "user_name", "label": "Your name", "type": "text",
     "help": "What the agent calls you.", "default": "User", "required": True},
    {"key": "timezone", "label": "Time zone", "type": "text",
     "help": "An IANA name, such as America/New_York.", "default": "UTC", "required": True,
     "zone": True},
    {"key": "user_bio", "label": "About you", "type": "text", "default": "", "max_len": 2000},
    {"key": "smart_search_refresh_minutes", "label": "Update the index every",
     "type": "number", "unit": "minutes", "min": 0, "max": 1440,
     "help": "0 stops the updates.", "default": 15},
    {"key": "lock_archived", "label": "Lock archived chats", "type": "bool",
     "help": "An archived chat is left alone until you write in it.", "default": True},
    {"key": "image_captions_auto", "label": "Describe new chat photos",
     "type": "bool",
     "help": "The photo description makes a photo searchable by what it shows.",
     "default": False},
    {"key": "code_websearch", "label": "Web search for the coding agent", "type": "bool",
     "help": "Its searches go to Exa, a third party.", "default": False},
    # server/celine/guard: read at every call, so a change applies at once; off is the
    # Guard feature switch (Settings, Features)
    {"key": "guard", "label": "Guard mode", "type": "choice",
     "choices": [["watch", "Watch"], ["on", "On"]],
     "help": "Watch: every call runs at once and Bastet writes down what she would have "
             "done. On: she decides before a call runs.",
     "default": "watch"},
    # Advanced: whole numbers, read where they are used (turns, jobs, uploads, backups)
    *({"key": k, "label": label, "type": "number", "unit": unit, "min": lo, "max": hi,
       "default": d, "advanced": True, **({"help": tip} if tip else {})}
      for k, label, unit, lo, hi, d, tip in (
        ("max_concurrent_turns", "Turns at once", None, 1, 64, 6,
         "How many chats may generate at the same time."),
        ("max_tool_iterations", "Tool rounds per turn", None, 0, 100_000, 0,
         "0: no cap, only the turn time limit."),
        ("turn_wall_clock_seconds", "Turn time limit", "seconds", 1, 86_400, 1800,
         "The agent's work time in one turn, tools included."),
        ("last_step_seconds", "Last answer time", "seconds", 1, 3600, 180,
         "After the turn time limit: one step without tools to answer with what is there."),
        ("tool_timeout_seconds", "Tool time limit", "seconds", 1, 86_400, 180, None),
        ("routine_max_minutes", "Routine time limit", "minutes", 0, 100_000, 180,
         "One routine run at most. 0: no cap."),
        ("mission_max_hours", "Mission time limit", "hours", 0, 100_000, 0,
         "One mission at most, from its start. 0: no cap."),
        ("mission_max_sub_agents", "Sub-agents per mission", None, 0, 10_000, 0,
         "0: no cap."),
        ("job_timeout_seconds", "Job time limit", "seconds", 1, 604_800, 3600,
         "A tool that runs in the background is stopped after this."),
        ("embed_query_timeout_seconds", "Search embedding time limit", "seconds", 1,
         3600, 60, "A search waits this long for its embedding model."),
        ("busy_retry_seconds", "Busy model wait", "seconds", 1, 86_400, 10,
         "Background work asks a busy model again after this, twice as long each time."),
        ("busy_retry_max_seconds", "Longest busy model wait", "seconds", 1, 86_400, 600,
         None),
        ("history_messages", "Messages per request", "messages", 0, 100_000, 200,
         "The most messages one request carries. 0: no cap."),
        ("events_keep", "Log entries per chat", "entries", 0, 1_000_000, 500,
         "Kept once its turn is over."),
        ("backup_keep_days", "Backups kept", "days", 1, 3650, 7, None),
        ("max_attachments", "Attachments per message", None, 0, 1000, 10, None),
        ("max_upload_mb", "Largest upload", "MB", 1, 1_000_000, 512,
         "One file added to Documents."),
        ("max_attachment_bytes", "Largest attachment", "bytes", 1, 10_000_000_000,
         20_000_000, None),
        ("inline_text_max_bytes", "Largest text read inline", "bytes", 0, 100_000_000,
         200_000, "A larger text file is attached, not put into the message."),
        ("max_message_bytes", "Largest message", "bytes", 1, 100_000_000, 1_000_000, None),
        ("requests_keep", "Model requests kept", "days", 0, 36_500, 0,
         "What each step sent to its model. 0: kept forever."),
    )),
    # Advanced switches, read where they are used (schedules, missions)
    *({"key": k, "label": label, "type": "bool", "default": d, "advanced": True,
       "help": tip}
      for k, label, d, tip in (
        ("routines_wait_for_missions", "Routines wait for missions", False,
         "A routine waits while a mission goes on by itself."),
        ("missions_resume_after_unfreeze", "Missions go on after Unfreeze", True,
         "A mission a freeze stopped carries on by itself."),
    )),
]
_BY_KEY = {f["key"]: f for f in FIELDS}


def value(general: dict[str, Any], key: str) -> Any:
    """A general setting, or its default when it was never saved."""
    return general.get(key, _BY_KEY[key]["default"])


def validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise Invalid("general", "General settings are an object.")
    out: dict[str, Any] = {}
    for k, v in data.items():
        f = _BY_KEY.get(k)
        path = f"general.{k}"
        if f is None:
            raise Invalid(path, f"'{k}' is not a general setting.")
        if v is None:
            continue
        if f["type"] == "bool":
            if not isinstance(v, bool):
                raise Invalid(path, "This must be true or false.")
        elif f["type"] == "choice":
            if v not in [c[0] for c in f["choices"]]:
                raise Invalid(path, "This must be one of " +
                              ", ".join(c[0] for c in f["choices"]) + ".")
        elif f["type"] == "number":
            if not isinstance(v, int) or isinstance(v, bool):
                raise Invalid(path, "This must be a whole number.")
            if v < f.get("min", v) or v > f.get("max", v):
                raise Invalid(path, f"This must be between {f['min']} and {f['max']}.")
        elif f["type"] == "text":
            if not isinstance(v, str):
                raise Invalid(path, "This must be text.")
            if f.get("required") and not v.strip():
                raise Invalid(path, "This cannot be empty.")
            if len(v) > f.get("max_len", len(v)):
                raise Invalid(path, f"This is at most {f['max_len']} characters.")
            if f.get("zone"):
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(v)
                except Exception:
                    raise Invalid(path, f"'{v}' is not a time zone. Try America/New_York.") \
                        from None
        out[k] = v
    if value(out, "job_timeout_seconds") <= value(out, "tool_timeout_seconds"):
        raise Invalid("general.job_timeout_seconds", "The job time limit must be longer "
                                                      "than the tool time limit.")
    if value(out, "busy_retry_max_seconds") < value(out, "busy_retry_seconds"):
        raise Invalid("general.busy_retry_max_seconds", "The longest busy model wait "
                                                         "must be at least the first.")
    return out
