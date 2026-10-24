"""The opaque cursor of a paged list: the sort key of the last row of a page."""
from __future__ import annotations

import base64
import json

from ..errors import CelineError


def page_cursor(values: tuple) -> str:
    """The opaque `next` of a page: the sort key of its last row."""
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode().rstrip("=")


def read_cursor(cursor: str | None) -> tuple[int, str, str] | None:
    if not cursor:
        return None
    try:
        r, t, i = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return int(r), str(t), str(i)
    except (ValueError, TypeError):
        raise CelineError("bad_request", "cursor is not one this server gave out", 400) \
            from None


def read_id(cursor: str | None) -> int | None:
    """The opaque `next` of a page whose sort key is one id."""
    if not cursor:
        return None
    try:
        (i,) = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return int(i)
    except (ValueError, TypeError):
        raise CelineError("bad_request", "cursor is not one this server gave out", 400) \
            from None
