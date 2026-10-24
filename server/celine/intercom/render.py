"""The line it reads above an intercom message. Rendered from the row at request time,
never stored: the message itself holds the bare text."""
from __future__ import annotations

from ..core.ids import when

LABEL = {"message": "From", "brief": "Brief from", "report": "Report from",
         "trigger": "Trigger from", "notice": "Notice from"}


def who(iv: dict, user: str) -> str:
    """Who wrote message `iv`, as it reads it: you, the user's name, or the sender."""
    if iv.get("mine"):
        return "you"
    if iv["from"] == "user":
        return user
    return iv.get("from_name") or iv["from"]


def header(iv: dict | None, user: str, tz) -> str:
    """"[From scout (#412, re #409, "logs")]\\n", "[Job 3f2a1b9c (shell__type) ended]\\n":
    the one marking style of what the app adds (turns/legend.py). Empty for the user's own message as typed, and for a message without a row."""
    if not iv:
        return ""
    kind, data = iv["kind"], iv.get("data") or {}
    if kind == "message" and iv["from"] == "user" and not iv.get("due_at") \
            and not iv.get("reply_to"):
        return ""                      # the user's own message, as typed
    if kind == "job":
        ids = ", ".join(j[:8] for j in data.get("jobs") or [])
        tools = ", ".join(data.get("tools") or [])
        word = "Job" if len(data.get("jobs") or []) < 2 else "Jobs"
        return f"[{word} {ids} ({tools}) {'stopped' if data.get('stopped_by') else 'ended'}]\n"
    bits = [f"#{iv['id']}"]
    if iv.get("reply_to"):
        bits.append(f"re #{iv['reply_to']}")
    if iv.get("subject"):
        bits.append(f'"{iv["subject"]}"')
    if data.get("time_limit"):
        bits.append("time limit")
    elif iv.get("due_at") and tz is not None:
        bits.append(f"set {when(iv['created_at'], tz)}")
    if iv.get("until_at") and tz is not None:
        bits.append(f"until {when(iv['until_at'], tz)}")
    if kind == "report" and data.get("state") not in (None, "done"):
        bits.append(f"its turn {data['state']}")
    label = "Brief changed by" if kind == "brief" and data.get("changed") else LABEL[kind]
    return f"[{label} {who(iv, user)} ({', '.join(bits)})]\n"
