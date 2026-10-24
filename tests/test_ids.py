"""core/ids.py: a stored time as it reads it, in the app zone."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from celine.core import ids


def test_a_stored_time_reads_in_the_app_zone():
    tz = ZoneInfo("Europe/Berlin")
    assert ids.when("2026-10-22T18:00:00.000+00:00", tz) == "Thu 22 Oct 20:00"
    due = ids.parse_when(None, "09:30", tz, datetime(2026, 10, 22, 6, tzinfo=tz))
    assert ids.when(ids.iso(due), tz) == "Thu 22 Oct 09:30"
