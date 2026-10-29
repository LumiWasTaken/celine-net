"""MCP servers, one supervisor task each. Each server is a skill named after its key
in mcp.toml with one tool per tool it advertises. The app is only ever an MCP client.

Three things a server block can add:
- `command` next to `url` (streamable HTTP): the supervisor starts the command when
  the url does not answer and nothing it started is still running, and never
  otherwise. The process outlives the app (its own session); in Docker the block has
  no command and compose runs the service.
- `kb = "<archive>"`: a connector (discord-link, wa-link). Records in a tool result's
  structured content are applied to that archive through the feed (kb/feed.py) and
  the result is rendered as Messages archive lines ` [kb:id] <when> <who>: <text>`, so they
  can be cited like any archived message. Messages references are accepted in place
  of the connector's own ids: a chat reference for `chat`, a message reference for
  `message`, `before` and `after` (its chat fills `chat`, which is then optional), an
  account reference for `user_id` and `from_user`. A connector only reads: a tool of
  its that is not read-only is not offered to Celine.
- `guides/<server>.md` (skills/guides): the skill's description and guide.

A plugin's `[servers.<id>]` blocks (plugin.toml, the same keys) run under its runner
(api/plugins.py) while it is on: `plugin_servers` fills each `${KEY}` from its form, and
its `guides/<server>.md` is that skill's guide.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import anyio
import httpx
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters, get_default_environment
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from . import attachments
from .config import ROOT, Problem, checked
from .core.ids import now
from .kb.identity import REF
from .errors import TooLarge, unreachable_words
from .skills import BUILTIN, GUIDES_DIR, META, Annotations, ToolContext, ToolResult, ToolSpec, guide

log = logging.getLogger("celine.mcp")

CLASSES = ("public", "informal", "nsfw", "sensitive")
SENDS = ("public", "people", "house")
KEYS = {                    # mcp.toml [servers.<id>]: McpServerConfig
    "enabled": "bool", "transport": "str", "command": "str", "args": "list", "env": "dict",
    "url": "str", "headers": "dict", "startup_timeout_seconds": "num", "max_restarts": "int",
    "kb": "str", "class": CLASSES, "sends": SENDS,
}


@dataclass
class McpServerConfig:
    id: str
    enabled: bool = False
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    startup_timeout_seconds: float = 20.0
    max_restarts: int = 5
    # a connector (discord-link, wa-link): the Messages archive its tool results and its
    # feed are written to (kb/feed.py)
    kb: str | None = None
    # Bastet (guard/facts.py): who may see its results (`class`: public, informal,
    # nsfw, sensitive) and where its writing tools send (public, people, house)
    cls: str = "sensitive"
    sends: str = "public"


_ENV_REF = re.compile(r"\$\{(\w+)(?:(:?)-([^}]*))?\}")


def _env_ref(value: str, keys: dict[str, str]) -> str:
    """`${VAR}`, `${VAR:-default}` (the default when unset or empty) and
    `${VAR-default}` (the default only when unset) from the environment; `keys`
    (keys.env) answers for a name the environment lacks."""
    def one(m: re.Match) -> str:
        v = os.environ.get(m.group(1), keys.get(m.group(1)))
        if m.group(3) is None:
            return v or ""
        return v if (v if m.group(2) else v is not None) else m.group(3)
    return _ENV_REF.sub(one, value)


def read_servers(file: str, raw: dict[str, Any], problems: list[Problem],
                 keys: dict[str, str]) -> dict[str, McpServerConfig]:
    """mcp.toml `[servers.<id>]` blocks, every text value's `${...}` filled. A block
    named like a built-in skill would replace it in the registry: left out, said."""
    for name in raw:
        if name != "servers":
            problems.append(Problem(file, f"unknown table [{name}], ignored"))
    servers = raw.get("servers", {})
    out: dict[str, McpServerConfig] = {}
    built_in = {META, *(name for name, _, _ in BUILTIN)}
    for sid, s in (servers if isinstance(servers, dict) else {}).items():
        if sid in built_in:
            problems.append(Problem(file, f"[servers.{sid}] is the name of a built-in skill; "
                                          f"that server is not started"))
            continue
        s = checked(file, f"servers.{sid}", s, KEYS, problems)
        if "class" in s:
            s["cls"] = s.pop("class")
        for k in ("command", "url"):
            if s.get(k):
                s[k] = _env_ref(s[k], keys) or None
        if "args" in s:
            s["args"] = [os.path.expanduser(_env_ref(str(a), keys)) for a in s["args"]]
        for k in ("env", "headers"):
            if k in s:
                s[k] = {str(n): _env_ref(str(v), keys) for n, v in s[k].items()}
        out[sid] = McpServerConfig(id=sid, **s)
    return out


_FIELD_REF = re.compile(r"\$\{(\w+)\}")


def plugin_servers(cfg, p) -> tuple[dict[str, McpServerConfig], dict[str, str]]:
    """Plugin `p`'s [servers.<id>] blocks, each `${KEY}` filled from its form: the field
    whose key is `key` (a value as saved, a secret as stored). -> (the servers to start,
    the sentence why each other one is not started: a value it needs is empty, its block
    is wrong, or its id is taken)."""
    from .settings import form
    fields = (p.form or {}).get("fields", [])
    by_name = {f["key"].upper(): f for f in fields}
    saved = form.values(fields, (getattr(cfg.settings, "plugins", None) or {}).get(p.id, {}))
    taken = {META, *(name for name, _, _ in BUILTIN), *cfg.mcp}
    out: dict[str, McpServerConfig] = {}
    why: dict[str, str] = {}
    for sid, raw in (p.manifest.get("servers") or {}).items():
        problems: list[Problem] = []
        s = checked(f"plugins/{p.id}/plugin.toml", f"servers.{sid}", raw, KEYS, problems)
        empty: list[str] = []

        def one(m: re.Match) -> str:
            f = by_name.get(m.group(1))
            if f is None:
                empty.append(f"{m.group(0)} names no field")
                return ""
            got = form.read_secret(cfg, p.id, f["key"]) if f["type"] == "secret" \
                else saved.get(f["key"])
            if got is None or str(got) == "":
                empty.append(f"{f['label']} is empty")
                return ""
            return str(got)

        if "class" in s:
            s["cls"] = s.pop("class")
        for k in ("command", "url"):
            if k in s:
                s[k] = _FIELD_REF.sub(one, s[k])
        if "args" in s:
            s["args"] = [_FIELD_REF.sub(one, str(a)) for a in s["args"]]
        for k in ("env", "headers"):
            if k in s:
                s[k] = {str(n): _FIELD_REF.sub(one, str(v)) for n, v in s[k].items()}
        if sid in taken:
            why[sid] = f"{sid} is the name of another skill or server."
        elif problems:
            why[sid] = f"{problems[0].message}."
        elif empty:
            why[sid] = f"{'; '.join(dict.fromkeys(empty))}."
        elif not s.get("enabled"):
            why[sid] = f"[servers.{sid}] is not enabled."
        else:
            out[sid] = McpServerConfig(id=sid, **s)
    return out, why


class McpSkill:
    def __init__(self, cfg: McpServerConfig, guides: Path | None = None):
        """`guides`: its plugin's folder of guides; else the skills' guides/."""
        self.cfg = cfg
        self.name = cfg.id
        if guides is None:
            self.guide = guide(cfg.id) if (GUIDES_DIR / f"{cfg.id}.md").is_file() else None
        else:
            self.guide = guide(cfg.id, file=str(guides / "_"), rel=f"{cfg.id}.md") \
                if (guides / f"{cfg.id}.md").is_file() else None
        self.description = (self.guide.description if self.guide
                            else f"Tools of the MCP server {cfg.id}.")
        self.status = "starting"        # starting | up | down (retrying) | dead
        self.error: str | None = None
        self.restarts = 0               # failures in a row (_supervise)
        self.ups = 0                    # times it came up
        self.client: Client | None = None
        self.kick = anyio.Event()       # set to force a reconnect
        self.proc: Any = None           # the process this app started, if any
        self.hidden: set[str] = set()   # a connector's writes: never offered


def _tools(listed) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description or "",
             "schema": t.input_schema,
             "annotations": _hint_dict(getattr(t, "annotations", None))}
            for t in listed.tools]


def _hint_dict(a: Any) -> dict[str, Any] | None:
    """The SDK's annotations object as the wire's camelCase hints (Annotations.from_mcp
    reads those; the object's own attributes are snake_case)."""
    if a is None or isinstance(a, dict):
        return a
    return a.model_dump(by_alias=True, exclude_none=True)


def _hints(t: dict[str, Any]) -> Annotations:
    return Annotations.from_mcp(t.get("annotations"))


class Supervisor:
    def __init__(self, cfg, store, registry, kbs=None, ping_seconds: float = 15.0):
        self.cfg = cfg
        self.store = store
        self.registry = registry
        self.kbs = kbs
        self.ping_seconds = ping_seconds
        self.skills: dict[str, McpSkill] = {}

    async def start(self, tg: anyio.abc.TaskGroup) -> None:
        """Every enabled server that no feature owns; a feature's (features.connector_owners)
        runs through `run_one` under its runner (core/runners.py)."""
        from .settings import features
        owned = features.connector_owners()
        for scfg in self.cfg.mcp.values():
            if scfg.id in owned:
                continue
            skill = await self._open(scfg)
            if skill is not None:
                tg.start_soon(self._run, skill)

    async def run_one(self, scfg: McpServerConfig | None, guides: Path | None = None) -> None:
        """One server, supervised until cancelled; its tools leave with it. `guides`: its
        plugin's guides folder."""
        skill = await self._open(scfg, guides)
        if skill is not None:
            await self._run(skill)

    async def _open(self, scfg: McpServerConfig | None,
                    guides: Path | None = None) -> McpSkill | None:
        """The skill registered with its cached tools; None when it runs already, or the
        server is not there or not enabled."""
        if scfg is None or not scfg.enabled or scfg.id in self.skills:
            return None
        skill = McpSkill(scfg, guides)
        self.skills[scfg.id] = skill
        self._register(skill, await cached_tools(self.store, scfg.id))
        return skill

    async def _run(self, skill: McpSkill) -> None:
        try:
            await self._supervise(skill)
        finally:
            self.skills.pop(skill.name, None)
            self.registry.remove(skill.name)
            if skill.proc is not None and skill.proc.returncode is None:
                with anyio.CancelScope(shield=True):
                    skill.proc.terminate()
                    with anyio.move_on_after(5):
                        await skill.proc.wait()

    def retry(self, name: str) -> bool:
        skill = self.skills.get(name)
        if skill is None:
            return False
        skill.restarts = 0
        skill.kick.set()
        return True

    def connector(self, kb_id: str) -> McpSkill | None:
        """The connector whose archive is `kb_id`."""
        return next((s for s in self.skills.values() if s.cfg.kb == kb_id), None)

    def _register(self, skill: McpSkill, tools: list[dict[str, Any]]) -> None:
        offered = tools
        if skill.cfg.kb:
            offered = [t for t in tools if _hints(t).read_only]
            skill.hidden = {t["name"] for t in tools} - {t["name"] for t in offered}
        self.registry.add_specs(skill, [self._spec(skill, t) for t in offered])

    # ---- the process -------------------------------------------------------------------
    async def _answers(self, url: str) -> bool:
        """Any HTTP answer counts; only no connection at all means it is not there."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as c:
                await c.get(url)
            return True
        except httpx.HTTPError:
            return False

    async def _ensure_running(self, skill: McpSkill) -> None:
        cfg = skill.cfg
        if not (cfg.command and cfg.url) or await self._answers(cfg.url):
            return
        if skill.proc is None or skill.proc.returncode is not None:
            logdir = self.cfg.data_dir / cfg.id
            logdir.mkdir(parents=True, exist_ok=True)
            logf = open(logdir / f"{cfg.id}.log", "ab")
            try:
                skill.proc = await anyio.open_process(
                    [cfg.command, *cfg.args], cwd=str(ROOT),
                    env={**get_default_environment(), **cfg.env},
                    stdout=logf, stderr=logf, start_new_session=True)
            finally:
                logf.close()
            log.info("mcp server started", extra={"event": "mcp.spawned", "server": cfg.id,
                                                  "pid": skill.proc.pid})
        with anyio.fail_after(cfg.startup_timeout_seconds):
            while not await self._answers(cfg.url):
                if skill.proc.returncode is not None:
                    raise RuntimeError(f"{cfg.command} exited ({skill.proc.returncode})")
                await anyio.sleep(0.2)

    # ---- the connection ----------------------------------------------------------------
    async def _supervise(self, skill: McpSkill) -> None:
        backoff = 1.0
        while True:
            skill.kick = anyio.Event()
            try:
                await self._ensure_running(skill)
                async with self._connect(skill.cfg) as client:
                    with anyio.fail_after(skill.cfg.startup_timeout_seconds):
                        listed = await client.list_tools()
                    tools = _tools(listed)
                    self._register(skill, tools)
                    await cache_tools(self.store, skill.name, tools)
                    skill.client, skill.status, skill.error = client, "up", None
                    backoff = 1.0
                    skill.restarts = 0
                    skill.ups += 1
                    log.info("mcp server up", extra={"event": "mcp.up",
                                                     "server": skill.name,
                                                     "tools": len(tools)})
                    await self._watch(skill, client)
            except anyio.get_cancelled_exc_class():
                raise
            except Exception as e:
                # a server of ours that quit says so; anything else is the server at its
                # address not answering (a connect error, a timeout, an ExceptionGroup)
                skill.error = str(e) if isinstance(e, RuntimeError) else \
                    unreachable_words(f"the {skill.name} server", skill.cfg.url, e)
                log.warning("mcp server failed", extra={"event": "mcp.failed",
                                                        "server": skill.name,
                                                        "error": skill.error})
            finally:
                skill.client = None

            skill.restarts += 1
            # a server this app starts a process for is dead past max_restarts; one at a
            # url alone comes back with its host, so it keeps reconnecting at the backoff
            if skill.cfg.command and skill.restarts > skill.cfg.max_restarts:
                skill.status = "dead"
                log.warning("mcp server dead", extra={"event": "mcp.dead",
                                                      "server": skill.name})
                await skill.kick.wait()
                skill.status = "starting"
                continue
            skill.status = "down"           # it failed: not reachable until it answers again
            with anyio.move_on_after(backoff):
                await skill.kick.wait()
            backoff = min(backoff * 2, 30.0)

    async def _watch(self, skill: McpSkill, client: Client) -> None:
        """Poll list_tools until the server stops answering or a call asks for a
        reconnect. Ping is deprecated in the SDK; list_tools every server has."""
        while True:
            with anyio.move_on_after(self.ping_seconds):
                await skill.kick.wait()
                raise RuntimeError("reconnect requested")
            with anyio.fail_after(10):
                listed = await client.list_tools()
            tools = _tools(listed)
            known = sorted([s.name for s in self.registry.tools.values()
                            if s.skill == skill.name] + list(skill.hidden))
            if sorted(t["name"] for t in tools) != known:
                self._register(skill, tools)
                await cache_tools(self.store, skill.name, tools)

    def _connect(self, cfg: McpServerConfig):
        timeout = float(self.cfg.r("tool_timeout_seconds"))
        if cfg.transport == "stdio":
            if not cfg.command:
                raise ValueError("stdio server has no command")
            params = StdioServerParameters(
                command=cfg.command, args=cfg.args,
                env={**get_default_environment(), **cfg.env})
            return Client(params, read_timeout_seconds=timeout)
        if cfg.transport in ("http", "streamable-http"):
            if not cfg.url:
                raise ValueError("http server has no url")
            transport = streamable_http_client(
                cfg.url, http_client=create_mcp_http_client(headers=cfg.headers or None))
            return Client(transport, read_timeout_seconds=timeout)
        raise ValueError(f"unknown transport {cfg.transport!r}")

    # ---- calls -------------------------------------------------------------------------
    def _spec(self, skill: McpSkill, t: dict[str, Any]) -> ToolSpec:
        name = t["name"]

        async def fn(ctx: ToolContext, **args) -> ToolResult:
            return await self._call(ctx, skill, name, args)

        schema = dict(t.get("schema") or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        if skill.cfg.kb:
            schema = _with_refs(schema, skill.cfg.kb)
        return ToolSpec(skill=skill.name, name=name, description=t.get("description", ""),
                        schema=schema, fn=fn, annotations=_hints(t))

    async def _call(self, ctx: ToolContext, skill: McpSkill, name: str,
                    args: dict) -> ToolResult:
        client = skill.client
        if client is None or skill.status != "up":
            return ToolResult(content=f"{skill.name} is {skill.status} right now.",
                              is_error=True)
        kb = self._kb(skill)
        if kb is not None:
            try:
                args = await anyio.to_thread.run_sync(_resolve_refs, kb, dict(args))
            except LookupError as e:
                return ToolResult(content=str(e), is_error=True)
        try:
            res = await client.call_tool(name, args)
        except Exception as e:
            log.warning("mcp call failed", extra={"event": "mcp.call_failed",
                                                  "server": skill.name, "tool": name,
                                                  "error": type(e).__name__})
            skill.kick.set()
            return ToolResult(content=f"{skill.name} is not answering: it went away during "
                                      f"this call and is reconnecting (config/mcp.toml names "
                                      f"its address).",
                              is_error=True)
        text = _text(res)
        # a file part (an image, audio, a resource) is kept for the call, as a browser
        # shot is (skills/browser.py _keep), and its row shows it; the model gets an
        # image with the result (turns/context.py), any other file as a line
        images = []
        limit = int(self.cfg.r("max_attachment_bytes"))
        for c in getattr(res, "content", None) or []:
            got = _file(c, name)
            if got is None:
                continue
            data, filename, media = got
            if len(data) > limit:
                text += f"\n{TooLarge(filename, len(data), limit).message} It was not kept."
                continue
            row = await attachments.save_generated(
                self.cfg, ctx.store, data, filename, media, ctx.tool_call_id or None,
                in_message=False)
            text += f"\n{row['filename']} ({attachments.size(row['bytes'])})"
            if row["kind"] == "image":
                images.append(row["id"])
            else:
                text += f", attached to this conversation for {self.cfg.user}."
        structured = getattr(res, "structured_content", None)
        display: dict[str, Any] = {"server": skill.name, "tool": name}
        is_error = bool(getattr(res, "is_error", False))
        if isinstance(structured, dict):
            for k in ("where", "note"):
                if structured.get(k):
                    display[k] = str(structured[k])
        if kb is not None and not is_error and isinstance(structured, dict) \
                and isinstance(structured.get("records"), list):
            text = await anyio.to_thread.run_sync(
                render, kb, skill.name, structured, self.cfg.user)
        elif not text and structured is not None:
            text = json.dumps(structured)
        return ToolResult(content=text.strip() or "[no content]", is_error=is_error,
                          display=display, images=images or None)

    def _kb(self, skill: McpSkill):
        if not skill.cfg.kb or self.kbs is None:
            return None
        return self.kbs.get(skill.cfg.kb)


_FILES = ("image", "audio", "resource")      # the parts that carry a file (_file)


def _text(res) -> str:
    parts = []
    for c in getattr(res, "content", None) or []:
        kind = getattr(c, "type", "")
        if kind not in _FILES:
            parts.append(c.text if kind == "text" else f"[{kind}]")
    return "\n".join(parts)


def _file(c, tool: str) -> tuple[bytes, str, str | None] | None:
    """A result part that carries a file: (its bytes, its file name, its media type or
    None to guess it); None for any other part."""
    kind = getattr(c, "type", "")
    if kind in ("image", "audio"):
        return (base64.b64decode(c.data),
                tool + (mimetypes.guess_extension(c.mime_type) or ""), c.mime_type)
    if kind != "resource":
        return None
    r = c.resource
    blob = getattr(r, "blob", None)
    data = base64.b64decode(blob) if blob is not None else r.text.encode()
    filename = unquote(urlsplit(str(r.uri)).path.rsplit("/", 1)[-1]) or tool
    return data, filename, r.mime_type


# ---- a connector's results as archive lines ------------------------------------------------

_CHAT_ARGS = ("chat",)
_MESSAGE_ARGS = ("message", "before", "after")
_PERSON_ARGS = ("user_id", "from_user")


def _with_refs(schema: dict[str, Any], kb_id: str) -> dict[str, Any]:
    """A connector tool's schema as Celine sees it: the arguments that take Messages archive
    references say so, and `chat` is optional where a message reference can give it."""
    props = {k: dict(v) if isinstance(v, dict) else v
             for k, v in (schema.get("properties") or {}).items()}
    kinds = {**{k: "chat" for k in _CHAT_ARGS}, **{k: "message" for k in _MESSAGE_ARGS},
             **{k: "account" for k in _PERSON_ARGS}}
    examples = {"chat": "68", "message": "48213", "account": "17"}
    for name, kind in kinds.items():
        p = props.get(name)
        if isinstance(p, dict):
            desc = str(p.get("description") or "").rstrip(". ")
            ref = f"a {kind} reference such as {kb_id}:{examples[kind]}"
            p["description"] = f"{desc}, or {ref}." if desc else f"{ref[0].upper()}{ref[1:]}."
    required = list(schema.get("required") or [])
    if "chat" in required and isinstance(props.get("chat"), dict) \
            and any(k in props for k in _MESSAGE_ARGS):
        required.remove("chat")
        props["chat"]["description"] += " Leave it empty when `message` is a reference."
    return {**schema, "properties": props, "required": required}


def _resolve_refs(kb, args: dict[str, Any]) -> dict[str, Any]:
    """Messages references (`<kb>:<id>`) in a connector call, as the connector's own ids:
    a chat reference in `chat` is the chat's key; a message reference in `message`,
    `before` or `after` is the message's key, and its chat fills `chat`; a person
    reference in `user_id` or `from_user` is the account's key. Raises LookupError with
    one sentence when a reference names nothing of that kind."""
    def ours(v: Any) -> int | None:
        m = REF.match(v.strip()) if isinstance(v, str) else None
        return int(m.group(2)) if m and m.group(1) == kb.id else None

    def one(sql: str, n: int):
        return kb.file.read_sync(lambda c: c.execute(sql, (n,)).fetchone())

    out = dict(args)
    chat_from_ref = None
    for k in _CHAT_ARGS:
        if (n := ours(out.get(k))) is not None:
            row = one("SELECT key FROM chats WHERE id = ?", n)
            if row is None:
                raise LookupError(f"{out[k]} is not a chat in the {kb.title} source. "
                                  f"`{k}` takes a chat reference (chat_archive__look_up). "
                                  f"With a message reference, leave `{k}` out.")
            chat_from_ref = out[k]
            out[k] = row["key"]
    for k in _MESSAGE_ARGS:
        if (n := ours(out.get(k))) is not None:
            row = one("SELECT c.key AS chat, m.key AS msg FROM messages m JOIN chats c "
                      "ON c.id = m.chat_id WHERE m.id = ?", n)
            if row is None:
                raise LookupError(f"{out[k]} is not a message in the {kb.title} source.")
            if out.get("chat") and out["chat"] != row["chat"]:
                raise LookupError(f"{out[k]} is not in the chat "
                                  f"{chat_from_ref or out['chat']}. Leave `chat` out: the "
                                  f"message reference gives it.")
            out[k] = row["msg"]
            out["chat"] = row["chat"]
    for k in _PERSON_ARGS:
        if (n := ours(out.get(k))) is not None:
            row = one("SELECT key FROM people WHERE id = ?", n)
            if row is None:
                raise LookupError(f"{out[k]} is not an account in the {kb.title} source. "
                                  f"`{k}` takes an account reference (chat_archive__look_up).")
            out[k] = row["key"]
    return out


def render(kb, server: str, structured: dict[str, Any], me: str) -> str:
    """Apply the result's records to the archive, then show them as archive lines.
    Lines of messages the archive does not keep (not in a synced chat's policy) are
    applied too: reading is archiving."""
    from .kb.feed import Consumer
    from .skills.archive import msg_line
    records = [r for r in structured.get("records") or [] if isinstance(r, dict)]
    lines: list[str] = []
    if structured.get("note"):
        lines.append(str(structured["note"]))
    if structured.get("where"):
        lines.append(f"where: {structured['where']}")
    if records:
        Consumer(kb).apply(records, cursor=False)
        keys = [((r.get("chat") or {}).get("key"), (r.get("message") or {}).get("key"))
                for r in records if r.get("kind") == "message"]
        ids = _archive_ids(kb, [k for k in keys if k[0] and k[1]])
        msgs = kb.messages_by_ids(sorted(set(ids.values()))) if ids else {}
        shown = sorted((msgs[i] for i in set(ids.values()) if i in msgs),
                       key=lambda m: (m["ts"], m["id"]))
        lines += [msg_line(kb.id, m, me) for m in shown]
        if not shown:
            lines.append("no messages")
    for k, arg in (("earlier", "before"), ("later", "after")):
        h = structured.get(k)
        if isinstance(h, dict) and h.get("chat") and h.get(arg):
            lines.append(f"{k}: {server}__read_messages(chat={json.dumps(str(h['chat']))}, "
                         f"{arg}={json.dumps(str(h[arg]))})")
    rest = {k: v for k, v in structured.items()
            if k not in ("records", "note", "where", "earlier", "later")}
    if rest:
        lines.append(json.dumps(rest, ensure_ascii=False))
    return "\n".join(lines)


def _archive_ids(kb, keys: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    if not keys:
        return {}

    def run(c) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = {}
        for chat, msg in keys:
            row = c.execute("SELECT m.id FROM messages m JOIN chats c ON c.id = m.chat_id "
                            "WHERE c.key = ? AND m.key = ?", (str(chat), str(msg))).fetchone()
            if row is not None:
                out[(chat, msg)] = row[0]
        return out
    return kb.file.read_sync(run)


# ---- the tool cache: what each server listed last, so its tools show while it starts ----

async def cache_tools(store, server: str, tools: list[dict]) -> None:
    def fn(c: sqlite3.Connection) -> None:
        c.execute("DELETE FROM mcp_tools WHERE server=?", (server,))
        for t in tools:
            c.execute("INSERT INTO mcp_tools(server,name,description,schema,annotations,seen_at)"
                      " VALUES(?,?,?,?,?,?)",
                      (server, t["name"], t.get("description", ""),
                       json.dumps(t.get("schema") or {}), json.dumps(t.get("annotations")),
                       now()))

    await store.write(fn)


async def cached_tools(store, server: str) -> list[dict]:
    def fn(c: sqlite3.Connection) -> list[dict]:
        rows = c.execute("SELECT name, description, schema, annotations FROM mcp_tools "
                         "WHERE server=? ORDER BY name", (server,)).fetchall()
        return [{"name": r["name"], "description": r["description"],
                 "schema": json.loads(r["schema"]),
                 "annotations": json.loads(r["annotations"] or "null")} for r in rows]

    return await store.read(fn)


# ---- Settings, Features: the connectors ---------------------------------------------------

async def _probe_connector(cfg, server: str, label: str) -> dict[str, str]:
    """A connector's /admin/status with the app's key: running (ready), nothing at its
    address (missing, with how to start it), another connector there that refuses the
    key (missing), or not in mcp.toml or off (unset). `label` is the app's name (Discord,
    WhatsApp), as the sentences say it."""
    from .kb.connectors import base_url
    s = cfg.mcp.get(server)
    if s is None or not s.enabled or not s.url:
        return {"state": "unset", "detail": f"{label} is not connected: its connector has no "
                                            f"address in config/mcp.toml."}
    base = base_url(s.url)
    host = urlsplit(base).netloc or base
    try:
        async with httpx.AsyncClient(timeout=2.5, headers=s.headers) as c:
            r = await c.get(f"{base}/admin/status")
    except httpx.HTTPError:
        return {"state": "missing", "detail": f"No {label} connector answers at {host}. Add "
                                              f"{server} to COMPOSE_PROFILES in .env and run "
                                              f"docker compose up -d."}
    if r.status_code in (401, 403):
        return {"state": "missing", "detail": f"A {label} connector answers at {host} but it "
                                              f"is not this app's."}
    if r.status_code != 200:
        return {"state": "missing", "detail": f"{label}'s connector at {host} answered "
                                              f"{r.status_code}."}
    try:
        state = (r.json().get("connection") or {}).get("state") or "running"
    except ValueError:
        state = "running"
    return {"state": "ready", "detail": f"{label} at {host} answers: {state}."}


async def probe_discord(cfg, services) -> dict[str, str]:
    return await _probe_connector(cfg, "discord", "Discord")


async def probe_whatsapp(cfg, services) -> dict[str, str]:
    return await _probe_connector(cfg, "whatsapp", "WhatsApp")
