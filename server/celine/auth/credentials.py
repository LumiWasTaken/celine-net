"""Every secret the login checks: setup and recovery codes, passwords and PINs.

One scrypt hash, stored as one blob (16 bytes of salt, then the 32-byte digest), and
one constant-time match. Codes and secrets differ only in what is hashed: a code goes
through normalize() first, a password or a PIN is hashed as typed.

A code is 28 characters of base32 (A to Z, 2 to 7) in seven groups of four, for
example `QX7M K2PD ...`: 140 random bits. Typing ignores case, spaces and hyphens.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from typing import Any, Iterable

CODE_CHARS = 28                 # 28 x 5 bits = 140 bits
GROUP = 4
RECOVERY_COUNT = 10

# scrypt: 16 MB and about 50 ms per check
_N, _R, _P = 2 ** 14, 8, 1
_SALT = 16
_NOT_BASE32 = re.compile(r"[^A-Z2-7]")
_BLANK = secrets.token_bytes(_SALT + 32)     # stands in for a missing row; nothing opens it


def new_code() -> str:
    raw = base64.b32encode(secrets.token_bytes(20)).decode()[:CODE_CHARS]
    return " ".join(raw[i:i + GROUP] for i in range(0, CODE_CHARS, GROUP))


def normalize(typed: str) -> str:
    """A typed code as the 28 characters that were hashed. 0 and 1 are read as O and I,
    which base32 leaves out for that reason."""
    return _NOT_BASE32.sub("", typed.upper().replace("0", "O").replace("1", "I"))


def hash(text: str, salt: bytes | None = None) -> bytes:
    salt = salt or secrets.token_bytes(_SALT)
    return salt + hashlib.scrypt(text.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)


def first_match(typed: str, rows: Iterable[tuple[Any, bytes]], at_least: int = 1) -> Any:
    """The ref of the first stored hash `typed` opens, or None. Hashes against every row,
    padded with blanks to `at_least`, so the time taken says nothing about which."""
    rows = list(rows)
    rows += [(None, _BLANK)] * (at_least - len(rows))
    found = None
    for ref, stored in rows:
        if hmac.compare_digest(hash(typed, stored[:_SALT]), stored) and found is None:
            found = ref
    return found
