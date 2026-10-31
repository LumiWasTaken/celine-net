"""The app's time zone (General settings), today's date in its prompt and the time now
in its tail."""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core.ids import now, when


def zone(cfg) -> ZoneInfo:
    """The app's time zone (General settings), UTC when it names none that exists."""
    try:
        return ZoneInfo(cfg.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def day_label(at: str, tz, now_: datetime) -> str:
    """The day of a stamp in the app's zone, as the Events list names it: Today, Yesterday,
    the weekday, "9 Oct", "October 2025"."""
    d = datetime.fromisoformat(at).astimezone(tz).date()
    today = now_.astimezone(tz).date()
    days = (today - d).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return d.strftime("%A")
    return f"{d.day} {d:%b}" if d.year == today.year else d.strftime("%B %Y")


def use_zone(cfg) -> None:
    """The process clock follows the app's time zone, so the Messages archive (SQLite's
    'localtime', plain datetimes) tells the same time as its other tools."""
    # note: one zone per process; the app has one user
    os.environ["TZ"] = str(zone(cfg))
    time.tzset()


def today(cfg) -> str:
    """Today's date for the system prompt. The date only: the prompt changes once a day,
    not every minute; each message's own stamp says its time, the tail says now
    (now_line)."""
    tz = zone(cfg)
    return f"Today is {datetime.now(tz):%A, %d %B %Y} ({tz})."


def now_line(cfg) -> str:
    """The time now for the tail of its request, rendered as the time line over every
    message (ids.when), and the one sentence that says which zone every time it reads
    is in. In the tail, not the system prompt: it changes every turn, the prefix must
    not."""
    tz = zone(cfg)
    return (f"Now it is [{when(now(), tz)}]. Every time you read here, over a message, "
            f"in a tool result, in the Intercom or on a note, is {tz} time, like this one.")
