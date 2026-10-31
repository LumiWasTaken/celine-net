"""Her Mac: setup.sh served to the Mac, the report it posts back saved into the form,
her scripts' messages to her, the setup line the form shows, and Check.

- The setup line, `curl -fsSL <app>/api/plugins/mac/setup/<token> | bash`: the form and
  her settings__read ask for it while the Mac has reported no Address, or after Set up
  again until it reports, and each ask makes a new token that works once within
  SETUP_SECONDS. The app's address is its stored one (settings/address.py); while that is
  localhost the form says how to publish the app on the network and asks for its address
  instead. Once the Mac reported, the form says Connected with what it said of itself.
- GET /api/plugins/mac/setup/<token>: setup.sh with the app's address and the link key
  filled in. GET /api/plugins/mac/setup.sh: the script as it is (`--status`).
- POST /api/plugins/mac/gateway with `Authorization: Bearer <link key>`: setup.sh's
  report line {address, token, agents, share, mac, ...}; the address, the token, the
  drones' and the share server's addresses and the Mac's facts (`mac`: its user, home,
  chip, memory, macOS, network; About This Mac, in her system prompt through
  devices.about) are saved into the form (celine/saves.py plugin_form), so the servers start
  again with them.
- POST /api/plugins/mac/events with the link key: {text, conversation?}, a message from
  her to herself (intercom__send to "me") in that conversation, else her newest chat: it
  starts her turn there, or reaches the one running.
These are `open` in plugin.toml: the Mac has no session. The link key is made once
(var/secrets/mac.link.key) and goes to the Mac only inside the script a token gets; it is
also the Mac's key to the app's model proxy (plugin.toml [drones])."""
from __future__ import annotations

import hmac
import secrets as random
import shlex
import time
from pathlib import Path
from typing import Any

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from celine import plugins
from celine.api.deps import json_body, services_of
from celine.saves import plugin_form
from celine.errors import NotFound
from celine.mcp import plugin_servers
from celine.settings import address, form, secrets
from celine.settings.files import Invalid

LINK = "link"                       # its secret: var/secrets/mac.link.key
SCRIPT = Path(__file__).with_name("setup.sh")
PRESET = "APP='' KEY='' JSON='' STATUS=''"      # setup.sh's line a served copy fills in
SETUP_SECONDS = 3600
LOCAL = ("This page is open at localhost, and her Mac cannot reach that. First run the line "
         "below on this computer, in the Celine folder. Then type the address her Mac can "
         "reach, such as http://192.168.1.20:8766, and press Check. The setup line for the "
         "Mac shows up here.")
PUBLISH = "echo CELINE_PUBLISH=8766 >> .env && docker compose up -d"
WAITING = "Run the setup line in the Terminal on the Mac. This page shows when it is connected."
NOT_YET = "Not connected yet."
_tokens: dict[str, float] = {}      # a setup line's token -> when it stops working
_again: set[str] = set()            # plugins whose line Set up again shows until a report
NAMES = {"macscreen": "Screen", "macshell": "Shell", "macbrowser": "Browser",
         "macfiles": "Files"}

router = APIRouter()


def _link(cfg) -> str | None:
    return form.read_secret(cfg, "mac", LINK)


async def run(services, p) -> None:
    """The link key, made once."""
    root = services.cfg.settings.secrets_root
    if root is not None and _link(services.cfg) is None:
        secrets.set_key(root, form.secret_name(p.id, LINK), random.token_hex(32))


async def copies(services, p) -> dict[str, str]:
    """{"command": the setup line with a new token, "note": WAITING}; {"note": LOCAL,
    "address": "ask"} while the app's address is localhost; once the Mac reported its
    Address (and no Set up again since), {"note": "Connected: <about>."} followed by each
    grant the screen server says is off (the form's note and her settings__read)."""
    cfg = services.cfg
    if _link(cfg) is None:
        return {}
    saved = cfg.settings.plugins.get(p.id) or {}
    if saved.get("url") and p.id not in _again:
        try:
            with anyio.fail_after(GRANTS_SECONDS):
                off = await _off(services)
        except Exception:                   # Check says why the screen does not answer
            off = []
        out: dict[str, Any] = {"facts": [{"label": label, "value": saved[k]}
                                         for k, label in FACTS if saved.get(k)]}
        if off:
            out["note"] = "Connected. " + turn_on([x["pane"] for x in off])
            out["lines"] = [{"text": x["text"], "tone": x["tone"]} for x in off]
        else:
            out["note"] = "Connected."
        return out
    app = address.base(cfg)
    if address.is_loopback(app):
        return {"note": LOCAL, "address": "ask", "publish": PUBLISH, "hide": ["again"]}
    now = time.monotonic()
    for t in [t for t, end in _tokens.items() if end < now]:
        del _tokens[t]
    token = random.token_urlsafe(32)
    _tokens[token] = now + SETUP_SECONDS
    return {"command": f"curl -fsSL {shlex.quote(f'{app}/api/plugins/{p.id}/setup/{token}')}"
                       f" | bash", "note": WAITING, "hide": ["again"]}


async def again(services, p, _input) -> dict[str, Any]:
    """Set up again: the form shows the setup line until the Mac reports anew."""
    _again.add(p.id)
    return {"ok": True, "sentence": ""}


@router.get("/setup.sh")
async def setup_sh() -> FileResponse:
    return FileResponse(SCRIPT, media_type="text/x-shellscript", filename="setup.sh")


@router.get("/setup/{token}")
async def setup_line(request: Request, token: str) -> Response:
    """setup.sh with the app's address and the link key, once per token."""
    services = services_of(request)
    end = _tokens.pop(token, None)
    key = _link(services.cfg)
    if end is None or end < time.monotonic() or key is None:
        raise NotFound(request.url.path)
    return Response(filled(address.base(services.cfg), key),
                    media_type="text/x-shellscript", headers={"cache-control": "no-store"})


def filled(app: str, key: str) -> str:
    """setup.sh with `app` and `key` in its PRESET line; never a copy without them."""
    text = SCRIPT.read_text()
    if text.count(PRESET) != 1 or not app or not key:
        raise RuntimeError("setup.sh has no PRESET line to fill")
    return text.replace(PRESET, f"APP={shlex.quote(app)} KEY={shlex.quote(key)} "
                                f"JSON='' STATUS=''")


def _linked(request: Request):
    """The services, when the request carries the link key; else 404."""
    services = services_of(request)
    key = _link(services.cfg)
    sent = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if key is None or not hmac.compare_digest(sent.encode(), key.encode()):
        raise NotFound(request.url.path)
    return services


@router.post("/gateway")
async def gateway(request: Request) -> dict[str, Any]:
    services = _linked(request)
    body = await json_body(request, need=("address", "token"))
    if not isinstance(body["address"], str) or not isinstance(body["token"], str):
        raise Invalid("body", "The address and the token are text.")
    p = plugins.get("mac")
    values = {**services.cfg.settings.plugins.get(p.id, {}), "url": body["address"]}
    for key in ("agents", "share"):
        if isinstance(body.get(key), str):
            values[key] = body[key]
    if isinstance(body.get("mac"), dict) and (said := about(body["mac"])):
        values["about"] = said
        values.update(facts(body["mac"]))
    out = await plugin_form(services, p, values, {"token": body["token"]})
    _again.discard(p.id)
    return out


ABOUT_CHARS = 80   # the most of one fact of the Mac
# the facts the form shows, each a field the Mac fills (settings.toml), in this order
FACTS = (("chip", "Chip"), ("memory", "Memory"), ("macos", "macOS"), ("network", "Network"),
         ("user", "User"))


def facts(mac: dict[str, Any]) -> dict[str, str]:
    """setup.sh's facts of the Mac as the form's values: {"chip": "Apple M4", "memory":
    "16 GB", "macos": "15.6", "network": "Wi-Fi (en0)", "user": "celine"}; a fact it did
    not send is left out."""
    def said(k: str) -> str:
        v = mac.get(k)
        return " ".join(v.split())[:ABOUT_CHARS] if isinstance(v, str) else ""
    memory = mac.get("memory")
    out = {"chip": said("chip"),
           "memory": f"{round(memory / 2**30)} GB"
           if isinstance(memory, int) and not isinstance(memory, bool) and memory > 0 else "",
           "macos": said("macos"),
           "network": said("network") + (f" ({said('interface')})" if said("interface") else ""),
           "user": said("user")}
    return {k: v.strip() for k, v in out.items() if v.strip()}


def about(mac: dict[str, Any]) -> str:
    """setup.sh's facts of the Mac in one line: "user celine, home /Users/celine, Apple
    M4, 16 GB memory, macOS 15.6, Wi-Fi (en0)"; a fact it did not send is left out."""
    def said(k: str) -> str:
        v = mac.get(k)    # from the Mac, into her system prompt: one line, short
        return " ".join(v.split())[:ABOUT_CHARS] if isinstance(v, str) else ""
    memory = mac.get("memory")
    parts = [f"user {said('user')}" if said("user") else "",
             f"home {said('home')}" if said("home") else "",
             said("chip"),
             f"{round(memory / 2**30)} GB memory"
             if isinstance(memory, int) and not isinstance(memory, bool) and memory > 0
             else "",
             f"macOS {said('macos')}" if said("macos") else "",
             said("network") + (f" ({said('interface')})" if said("interface") else "")]
    return ", ".join(x.strip() for x in parts if x.strip())


_NEWEST = ("SELECT id FROM conversations WHERE kind='chat' AND agent_of IS NULL AND "
           "trashed_at IS NULL AND archived_at IS NULL ORDER BY updated_at DESC LIMIT 1")


@router.post("/events")
async def events(request: Request) -> dict[str, Any]:
    services = _linked(request)
    body = await json_body(request, need=("text",), allow=("text", "conversation"))
    text, cid = body["text"], body.get("conversation")
    if not isinstance(text, str) or not text.strip():
        raise Invalid("text", "The text is the message.")
    if cid is None:
        row = await services.store.read(lambda c: c.execute(_NEWEST).fetchone())
        if row is None:
            raise NotFound("conversation")
        cid = row[0]
    elif not isinstance(cid, str):
        raise Invalid("conversation", "The conversation is its id.")
    try:
        out = await services.intercom.send(f"agent:{cid}", f"agent:{cid}", text)
    except KeyError:
        raise NotFound("conversation") from None
    except ValueError as e:
        raise Invalid("conversation", str(e)) from None
    return {"intercom_id": out["intercom_id"], "conversation": cid, "turn_id": out["turn_id"]}


async def check(services, p, _input) -> dict[str, Any]:
    """Each server as the app sees it now (its tools, or why it does not answer, as said),
    and each macOS permission the screen server says it lacks, with where it is granted."""
    if not (services.cfg.settings.plugins.get(p.id) or {}).get("url"):
        return {"ok": False, "sentence": "", "lines": [{"text": NOT_YET, "tone": "warn"}]}
    _, why = plugin_servers(services.cfg, p)
    lines: list[dict[str, str]] = []
    for sid in p.manifest["servers"]:
        name = NAMES.get(sid, sid)
        skill = services.mcp.skills.get(sid)
        if sid in why:
            lines.append({"text": f"{name}: {why[sid]}", "tone": "error"})
        elif skill is None or skill.status != "up":
            said = (skill.error if skill is not None else None) or "starting"
            lines.append({"text": f"{name}: {said}", "tone": "error"})
        else:
            n = sum(1 for t in services.registry.tools.values() if t.skill == sid)
            lines.append({"text": f"{name}: {n} tools", "tone": "ok"})
    try:
        off = await _off(services)
    except Exception as e:
        off = []
        lines.append({"text": f"{NAMES['macscreen']}: {e}", "tone": "error"})
    lines += [{"text": x["text"], "tone": x["tone"]} for x in off]
    ok = all(x["tone"] == "ok" for x in lines)
    return {"ok": ok, "sentence": turn_on([x["pane"] for x in off]) if off else "",
            "lines": lines}


APP = "Celine"         # setup.sh's ~/Applications/Celine.app: every server on the Mac runs under it
GRANTS_SECONDS = 5     # the form's note waits this long for the screen server
# Peekaboo's `_meta` key: the grant, what she cannot do without it, its pane, its tone
GRANTS = {
    "screen_recording": ("Screen Recording", "no screenshots",
                         "Screen & System Audio Recording", "error"),
    "accessibility": ("Accessibility", "no clicks or typing", "Accessibility", "error"),
    "event_synthesizing": ("Event Synthesizing", "no typing into apps in the background",
                           "Accessibility", "warn"),
}


def turn_on(panes: list[str]) -> str:
    """Where the user turns on APP for the grants that are off, each list named once."""
    lists = " and ".join(dict.fromkeys(panes))
    return (f"On the Mac, open System Settings, Privacy & Security, and turn on {APP} "
            f"under {lists}.")


async def _off(services) -> list[dict[str, str]]:
    """Each grant the screen server says is off; [] while it does not run."""
    screen = services.mcp.skills.get("macscreen")
    if screen is None or screen.client is None:
        return []
    return _grants(await screen.client.call_tool("permissions", {}))


def _grants(res) -> list[dict[str, str]]:
    """Each grant Peekaboo's permissions answer says is off (`_meta`, false): what she
    cannot do without it and where the user turns it on for APP."""
    meta = getattr(res, "meta", None) or {}
    return [{"text": f"{name} is off: {without}.", "tone": tone, "pane": pane}
            for key, (name, without, pane, tone) in GRANTS.items() if meta.get(key) is False]
