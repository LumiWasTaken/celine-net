"""The app's own address (var/settings/address.toml `origin`): the origin of the browser
in which the setup code was entered or, on an instance that has none yet, the first
login came from (auth/routes.py `_signed_in`); changed in Settings, Security. The login's
relying party (auth), the origin check (api/origin.py), share links (shares.py) and push
(push.py) read it here.

Until one is stored the open login routes accept a request whose Origin is its own Host
(`same_origin`), and the first login or setup stores it; every other route takes only
loopback (api/origin.py)."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .files import Invalid

LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def clean(origin: str) -> str | None:
    """`scheme://host[:port]`, lower case, or None when it is not one."""
    try:
        p = urlsplit(origin.strip())
        p.port                                  # a bad port raises
    except (ValueError, AttributeError):
        return None
    if (p.scheme not in ("http", "https") or not p.hostname or p.path not in ("", "/")
            or p.query or p.fragment or p.username or p.password):
        return None
    return f"{p.scheme}://{p.netloc.lower()}"


def validate(data: Any) -> dict[str, str]:
    if not isinstance(data, dict):
        raise Invalid("address", "The address is an object.")
    if extra := set(data) - {"origin"}:
        raise Invalid(f"address.{sorted(extra)[0]}", "The address has only an origin.")
    origin = data.get("origin") or ""
    if not origin:
        return {}
    c = clean(origin) if isinstance(origin, str) else None
    if c is None:
        raise Invalid("address.origin", "This must be an address, such as "
                                        "https://celine.example.net or http://192.168.1.20:8766.")
    return {"origin": c}


def get(cfg) -> str:
    """The stored origin, "" while none is."""
    s = getattr(cfg, "settings", None)
    return (s.address.get("origin", "") if s is not None else "") or ""


def base(cfg) -> str:
    """Where a link to the app points: the stored origin, else this machine."""
    return get(cfg) or f"http://localhost:{cfg.port}"


def is_loopback(origin: str) -> bool:
    host = urlsplit(origin).hostname or ""
    return host in LOOPBACK or host.startswith("127.")


def same_origin(headers) -> str | None:
    """The request's Origin when it names the request's own Host (a browser on the page it
    was served), else None. A browser sets both; a page cannot."""
    o = headers.get("origin")
    c = clean(o) if o else None
    return c if c and urlsplit(c).netloc == headers.get("host", "").lower() else None
