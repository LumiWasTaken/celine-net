"""Ids and times. One format everywhere."""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timedelta, timezone



def now() -> str:
    """UTC, ISO 8601, millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def now_ms() -> int:
    """Unix time in milliseconds."""
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex


def when(created_at: str, tz) -> str:
    """A stored timestamp as it reads it, in the app zone: "Fri 2 Oct 01:40"."""
    return datetime.fromisoformat(created_at).astimezone(tz).strftime("%a %-d %b %H:%M")


_PIECE = re.compile(r"(\d+)\s*(minutes|minute|min|m|hours|hour|h|days|day|d)")
_SECONDS = {"m": 60, "h": 3600, "d": 86400}
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}")


def parse_when(in_: str | None, at: str | None, tz, now_: datetime) -> datetime | None:
    """`in` is "30 min", "2 h", "1 h 30 min" or "3 days", with no upper limit; `at` is
    HH:MM today in the app zone, tomorrow once it has passed, or "YYYY-MM-DD HH:MM" in
    the app zone, still ahead. `in` first, then `at`; None when neither reads."""
    if in_:
        pieces = _PIECE.findall(in_)
        if pieces and not _PIECE.sub("", in_).strip():
            return now_ + timedelta(seconds=sum(int(n) * _SECONDS[u[0]] for n, u in pieces))
    if at and _DATE.fullmatch(at.strip()):
        try:
            due = datetime.strptime(at.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except ValueError:
            return None
        return due if due > now_ else None
    if at:
        m = _CLOCK.fullmatch(at.strip())
        if m and int(m[1]) < 24 and int(m[2]) < 60:
            local = now_.astimezone(tz)
            due = local.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
            if due <= local:
                due += timedelta(days=1)
            return due
    return None


def clock(due: datetime, tz, now_: datetime) -> str:
    """"01:40" today, "Sat 02:00" on another day."""
    local = due.astimezone(tz)
    if local.date() == now_.astimezone(tz).date():
        return local.strftime("%H:%M")
    return local.strftime("%a %H:%M")


def iso(t: datetime) -> str:
    """A time as the tables store it: UTC ISO like now()."""
    return t.astimezone(timezone.utc).isoformat(timespec="milliseconds")


HOUR = 3600.0


def hour_room(sent, cap: int, now: float) -> float:
    """A cap per hour over `sent` (a deque of monotonic times, oldest first): 0 when one
    more fits, counted into `sent` now; else the seconds until one fits. `cap` 0 is no
    limit."""
    if not cap:
        return 0.0
    while sent and now - sent[0] >= HOUR:
        sent.popleft()
    if len(sent) < cap:
        sent.append(now)
        return 0.0
    return sent[-cap] + HOUR - now


def reached(who: str, cap: int) -> str:
    """The sentence when `who` used its `cap` per hour (hour_room)."""
    return f"{who} reached its {cap} request{'' if cap == 1 else 's'} this hour."
