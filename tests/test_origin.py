"""The Origin guard. Without it any page open in the browser could read conversations or
POST a message that types into its terminal: cross-origin POSTs need no preflight and
the WebSocket handshake is not subject to CORS."""
from __future__ import annotations

from celine.api.origin import allowed_origins, check, ws_origin_ok
from celine.errors import ForbiddenOrigin

ALLOWED = allowed_origins(8765, dev=False)
HOME = "127.0.0.1:8765"


def refused(host, origin, method, allowed=ALLOWED):
    headers = {"host": host, **({"origin": origin} if origin else {})}
    try:
        check(headers, method, allowed)
    except ForbiddenOrigin:
        return True
    return False


def test_only_the_app_itself_may_call_the_api():
    assert not refused(HOME, f"http://{HOME}", "POST")
    assert not refused(HOME, None, "GET"), "no Origin: a plain read"
    assert refused(HOME, None, "POST"), "no Origin: never a write"
    assert refused(HOME, "http://evil.example", "POST")
    assert refused(HOME, "http://evil.example", "GET")
    assert refused("celine.example.com", f"http://{HOME}", "GET"), "DNS rebinding"
    dev = allowed_origins(8765, dev=True)
    assert not refused("127.0.0.1:5174", "http://127.0.0.1:5174", "POST", dev)
    assert refused("127.0.0.1:5174", "http://127.0.0.1:5174", "POST")
    assert ws_origin_ok({"host": HOME, "origin": f"http://{HOME}"}, ALLOWED)
    assert not ws_origin_ok({"host": HOME, "origin": "http://evil.example"}, ALLOWED)
