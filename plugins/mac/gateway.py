"""Her Mac: setup.sh served to the Mac, the report it posts back saved into the form,
her scripts' messages to her, the command the form shows, and Check.

- GET /api/plugins/mac/setup.sh: the script, so the Mac needs nothing but the app.
- POST /api/plugins/mac/gateway with `Authorization: Bearer <link key>`: setup.sh's
  report line {address, token, ...}; the address and the token are saved into the form
  (api/forms.py save), so the servers start again with them.
- POST /api/plugins/mac/events with the link key: {text, conversation?}, a message from
  her to herself (intercom__send to "me") in that conversation, else her newest chat: it
  starts her turn there, or reaches the one running.
All three are `open` in plugin.toml: the Mac has no session. The link key is made once
(var/secrets/mac.link.key) and is part of the command."""
from __future__ import annotations

import hmac
import re
import secrets as random
import shlex
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from celine import plugins
from celine.api.deps import json_body, services_of
from celine.api.forms import save
from celine.errors import NotFound
from celine.mcp import plugin_servers
from celine.settings import form, secrets
from celine.settings.files import Invalid

LINK = "link"                       # its secret: var/secrets/mac.link.key
SCRIPT = Path(__file__).with_name("setup.sh")
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


async def copies(services, p, request: Request) -> dict[str, str]:
    """The command: download setup.sh from this app, run it with this app's address and
    the link key."""
    key = _link(services.cfg)
    if key is None:
        return {}
    app = str(request.base_url).rstrip("/")
    url = shlex.quote(f"{app}/api/plugins/{p.id}/setup.sh")
    return {"command": f"curl -fsSLo setup.sh {url} && bash setup.sh --app "
                       f"{shlex.quote(app)} --key {key}"}


@router.get("/setup.sh")
async def setup_sh() -> FileResponse:
    return FileResponse(SCRIPT, media_type="text/x-shellscript", filename="setup.sh")


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
    return await save(services, p, values, {"token": body["token"]})


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
    screen = services.mcp.skills.get("macscreen")
    if screen is not None and screen.client is not None:
        try:
            lines += _grants(await screen.client.call_tool("permissions", {}))
        except Exception as e:
            lines.append({"text": f"{NAMES['macscreen']}: {e}", "tone": "error"})
    ok = all(x["tone"] == "ok" for x in lines)
    return {"ok": ok, "sentence": "", "lines": lines}


def _grants(res) -> list[dict[str, str]]:
    """Peekaboo's permissions answer: per grant it lacks (`_meta`, false), its status line
    and the "Grant via" line under it, as it says them."""
    meta = getattr(res, "meta", None) or {}
    text = _TAG.sub("", "\n".join(getattr(c, "text", "") for c in res.content or []))
    out = []
    for key, value in meta.items():
        title = key.replace("_", " ").title()             # screen_recording: Screen Recording
        status = next((ln for ln in text.splitlines() if ln.startswith(title + " (")), None)
        if value is not False or status is None:
            continue
        part = next((b for b in text.split("\n\n") if title in b and "Grant via:" in b), "")
        via = next((ln for ln in part.splitlines() if ln.startswith("Grant via:")), "")
        out.append({"text": f"{status}. {via}" if via else status,
                    "tone": "error" if "(Required)" in status else "warn"})
    return out


_TAG = re.compile(r"\[(?:err|warn|ok)\] ")
