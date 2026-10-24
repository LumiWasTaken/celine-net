"""No secret reaches a log, a response or an event payload."""
from __future__ import annotations

import json
import logging

from celine.errors import mask, redact, set_secrets
from celine.logs import JsonFormatter

CANARY = "sk-test-canary-do-not-log-4f2a9c8e"


def test_secrets_are_masked_known_unknown_and_longest_first():
    set_secrets([CANARY])
    assert CANARY not in redact(f"Authorization: Bearer {CANARY}")
    set_secrets([])
    for probe in ("sk-abcdefghijklmnopqrstuvwx", "ghp_0123456789abcdefghij",
                  "xoxb-1234567890-abcdefgh"):
        assert probe not in redact(f"token {probe}")
    # a short secret inside a longer one must not leave half of the longer legible
    set_secrets(["abcdefgh", "abcdefgh-ijklmnop"])
    assert redact("x abcdefgh-ijklmnop y") == "x **** y"
    # its arguments are raw JSON: a quote or a backslash in a secret is escaped there
    set_secrets(['ab"cd\\efgh'])
    assert mask(json.dumps({"text": 'ab"cd\\efgh'})) == '{"text": "****"}'
    # mask(): the known set only, so a token the user hands it still works
    assert mask("Bearer abcdefghijklmnop1234") == "Bearer abcdefghijklmnop1234"


def test_the_log_formatter_redacts_message_and_extras():
    set_secrets([CANARY])
    rec = logging.LogRecord("celine", logging.INFO, "f", 1, f"calling with {CANARY}",
                            None, None)
    rec.__dict__["header"] = f"Bearer {CANARY}"
    assert CANARY not in JsonFormatter().format(rec)
