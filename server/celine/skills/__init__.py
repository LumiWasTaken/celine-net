"""Skills and tools.

A skill is a Python class with `@tool` coroutines plus a guide, `guides/<skill>.md`
(SKILL.md style: frontmatter `name`, `description`, optional `words`, then the body).
What it has in context every turn: the skill index (the loaded skills, and one line per
other skill, from the frontmatter) and the tools of the loaded skills, a core skill's
with their parameters, another's by name and one line until it loads them
(turns/loads.py decides which, from CORE below and each guide's `words:`). A tool that is off or a skill
that is down stays in the list as a stub that says so (turns/prompt.py).
`skills__read_guide(name)` returns a skill's guide and its notes on it,
`skills__load(names)` loads skills and tools, `skills__unload(names)` lets them go.
"""
from __future__ import annotations

import inspect
import keyword
import logging
import re
import types
from pathlib import Path
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal, get_args, get_origin

from .. import plugins
from ..config import Problem
from ..settings import features

log = logging.getLogger("celine.skills")

SEP = "__"          # skill__tool. Double, so split("__", 1) round-trips.
META = "skills"     # the skill that shows the guides: skills__read_guide
BODY = {"shell", "code", "browser"}     # the skills that run on its body (ToolSpec.runs)
LINE_MAX = 200      # characters of a tool's line while its parameters are not loaded


@dataclass
class ToolResult:
    content: str                              # what the model sees
    display: dict[str, Any] | None = None     # structured payload for the UI row
    is_error: bool = False
    # attachment ids (saved for the call, not in_message) the result holds: images the
    # model gets with the result, as it gets a message's (turns/context.py)
    images: list[str] | None = None


@dataclass
class ToolContext:
    conversation_id: str
    turn_id: str
    tool_call_id: str
    store: Any
    config: Any
    # emit(note, **data): a progress line for the call's row (event tool.progress)
    emit: Callable[..., Awaitable[None]]
    # the most one result may take, in tokens (~30% of the model's window); a tool
    # that can cut at a natural boundary should, and say what it left out
    max_output_tokens: int | None = None
    # seconds left of the turn's work time, None when the turn has no clock; a tool
    # that waits (a shell watch) stops before it
    time_left: float | None = None
    # the notes loaded in full in this request (turns/loads.py): a prompt fragment
    # leaves them out of the notebook index
    loaded_notes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Annotations:
    """What a tool does to the world, as MCP's tool annotations (readOnlyHint,
    destructiveHint, openWorldHint). The runner derives from them what
    it needs: a read-only blocking tool may run concurrently and may run again after a
    restart; anything else may not."""
    read_only: bool = True
    destructive: bool = False       # may delete or overwrite (only when not read_only)
    open_world: bool = False        # reaches outside the app (the machine, a network)

    @classmethod
    def of(cls, *, read_only: bool = True, destructive: bool | None = None,
           open_world: bool = False) -> "Annotations":
        """Unset hints default as MCP's do: a tool that writes is assumed destructive
        unless it says otherwise."""
        if read_only:
            return cls(True, False, open_world)
        return cls(False, True if destructive is None else destructive, open_world)

    @classmethod
    def from_mcp(cls, hints: Any) -> "Annotations":
        """An MCP server's annotations (hints, untrusted): its defaults, the worst case."""
        def get(key, default):
            v = getattr(hints, key, None) if hints is not None and not isinstance(
                hints, dict) else (hints or {}).get(key)
            return default if v is None else bool(v)
        return cls.of(read_only=get("readOnlyHint", False),
                      destructive=get("destructiveHint", True),
                      open_world=get("openWorldHint", True))


@dataclass(frozen=True)
class ToolSpec:
    skill: str
    name: str
    description: str
    schema: dict[str, Any]
    fn: Callable[..., Awaitable[ToolResult]]
    mode: str = "blocking"
    annotations: Annotations = Annotations()
    background: str | None = None   # a detach tool's bool parameter: hand off at once
    timeout: float | None = None    # seconds, instead of tool_timeout_seconds
    # what it reads while a job has the call: {job} and the call's arguments filled in,
    # with `detached_display` on its row
    detached: str | None = None
    detached_display: dict[str, Any] | None = None
    # False: ran ok beside an answer, it needs none of its own (the turn ends with its
    # step, turns/runner.py)
    needs_answer: bool = True

    @property
    def runs(self) -> str:
        """Where it runs: "body" (its own machine: the shell, the browser) or "house"
        (its memory and sending, behind the guard). Today both are this app; the seam
        for its own machine later (docs/ARCHITECTURE.md "House and body")."""
        return "body" if self.skill in BODY else "house"

    @property
    def mutating(self) -> bool:
        """Not read-only: never run concurrently, never re-run after a restart."""
        return not self.annotations.read_only

    @property
    def wire_name(self) -> str:
        return f"{self.skill}{SEP}{self.name}"

    def as_wire(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.wire_name,
                             "description": self.description,
                             "parameters": self.schema}}

    @property
    def line(self) -> str:
        """Its description's first sentence, at most LINE_MAX characters."""
        first = re.split(r"(?<=[.!?])\s|\n", self.description.strip(), maxsplit=1)[0]
        return first if len(first) <= LINE_MAX else first[:LINE_MAX].rsplit(" ", 1)[0]

    @property
    def bare(self) -> bool:
        """It takes no parameters: its name form is its full form, with no load step."""
        return not self.schema.get("properties")

    def as_name(self) -> dict[str, Any]:
        """Its entry while its parameters are not loaded: the name, one line, no
        parameters (turns/loads.py); a bare tool in full."""
        if self.bare:
            return self.as_wire()
        return {"type": "function",
                "function": {"name": self.wire_name, "description": self.line,
                             "parameters": {"type": "object", "properties": {}}}}


Mode = Literal["blocking", "detach"]


def tool(*, mode: "Mode" = "blocking", read_only: bool = True,
         destructive: bool | None = None, open_world: bool = False, background: str | None = None,
         timeout: float | None = None,
         detached: str | None = None, detached_display: dict[str, Any] | None = None,
         needs_answer: bool = True):
    """Marks a coroutine as a tool. The docstring is the model-facing description:
    what it does, when to use it and when not, an example call, what comes back.
    A tool that changes anything says `read_only=False`. `background` names a bool
    parameter of a detach tool: true hands the call to a background job after
    BACKGROUND_AFTER seconds instead of the detach window (turns/tools.py). `timeout`
    replaces tool_timeout_seconds for a tool that is slow by nature. The rest: ToolSpec."""
    def deco(fn):
        fn.__celine_tool__ = {"mode": mode, "background": background, "timeout": timeout,
                              "detached": detached,
                              "detached_display": detached_display,
                              "needs_answer": needs_answer,
                              "annotations": Annotations.of(
            read_only=read_only, destructive=destructive, open_world=open_world)}
        return fn
    return deco


# ---- guides: SKILL.md-style docs, one per built-in skill ---------------------------
# skills/guides/<skill>.md, in the package, read once when first asked


class Guide:
    """guides/<name>.md: `---` frontmatter (name, description, and `words`: what a new
    user message says when it needs the skill, turns/loads.asked), then the body, with
    {{key}} filled from `fill` (a number the code owns, like browser's MAX_TABS). Read
    when asked: `body`, `description` and `words` follow the file."""

    def __init__(self, name: str, description: str | None = None, body: str = "",
                 words: tuple[str, ...] = (), fill: dict[str, Any] | None = None,
                 file: str = __file__, rel: str = ""):
        """With a `description`: a guide made of these words, no file (tests, a stub).
        `rel` beside `file`: where it is read (a plugin's guide.md); else guides/<name>.md."""
        self.name = name
        self._file, self._rel = file, rel or f"guides/{name}.md"
        self._fill = fill or {}
        self._parsed: tuple[str, dict[str, str], str] | None = None
        if description is not None:
            self._parsed = (None, {"description": description, "words": ", ".join(words)},
                            body)

    def _parse(self) -> tuple[dict[str, str], str]:
        if self._parsed is not None and self._parsed[0] is None:    # fixed words
            return self._parsed[1], self._parsed[2]
        from .. import prompts
        text = prompts.text(self._file, self._rel)
        if self._parsed is not None and self._parsed[0] == text:
            return self._parsed[1], self._parsed[2]
        filled = text
        for key, value in self._fill.items():
            filled = filled.replace("{{" + key + "}}", str(value))
        meta: dict[str, str] = {}
        body = filled
        if filled.startswith("---\n"):
            head, _, body = filled[4:].partition("\n---\n")
            key = None
            for line in head.splitlines():
                if line[:1].isspace() and key:          # a folded continuation line
                    meta[key] += " " + line.strip()
                elif ":" in line:
                    key, _, value = line.partition(":")
                    key = key.strip()
                    meta[key] = value.strip()
        if meta.get("name") != self.name:
            raise ValueError(f"{self._rel}: name must be {self.name!r}")
        self._parsed = (text, meta, body.strip())
        return meta, body.strip()

    @property
    def description(self) -> str:
        return self._parse()[0].get("description", "")

    @property
    def body(self) -> str:
        return self._parse()[1]

    @property
    def words(self) -> tuple[str, ...]:
        return tuple(w.strip().lower() for w in self._parse()[0].get("words", "").split(",")
                     if w.strip())


class described:
    """`description = described()` on a skill class: its guide's description line, read
    when asked (the guide file is read lazily, see guide())."""

    def __get__(self, obj, cls):
        return (cls or type(obj)).guide.description


GUIDES: list[Guide] = []        # every guide a skill declared, checked in preflight
GUIDES_DIR = Path(__file__).parent / "guides"


def guide(name: str, *, file: str = __file__, rel: str = "", **fill: Any) -> Guide:
    """A skill's guide. Read lazily: the config dir is checked first (preflight), then
    check_guides parses every one, so a missing or misnamed guide refuses the start. A
    plugin's skill names its own: `guide(name, file=__file__, rel="guide.md")`, checked
    when `build` makes the skill (a bad one keeps that plugin out, not the app)."""
    g = Guide(name, fill=fill, file=file, rel=rel)
    if file == __file__:
        GUIDES.append(g)
    return g


def check_guides() -> str | None:
    """The first missing or misnamed guide as a sentence, None when every one parses."""
    for g in GUIDES:
        try:
            g._parse()
        except (OSError, ValueError) as e:
            return f"{g._rel}: {e}"
    return None


def guide_text(cfg, body: str) -> str:
    """A guide's body as it reads it."""
    return body


def _filled(value: Any, cfg) -> Any:
    """Every string in a tool's schema with {{username}} and {{name}} filled in."""
    if isinstance(value, str):
        return cfg.substitute(value)
    if isinstance(value, list):
        return [_filled(v, cfg) for v in value]
    if isinstance(value, dict):
        return {k: _filled(v, cfg) for k, v in value.items()}
    return value


_SIMPLE: dict[Any, str] = {str: "string", int: "integer", float: "number", dict: "object",
                           list: "array", bool: "boolean"}


def _json_type(ann: Any) -> dict[str, Any]:
    if ann in _SIMPLE:
        return {"type": _SIMPLE[ann]}
    origin = get_origin(ann)
    if origin is Literal:
        opts = list(get_args(ann))
        kind = _SIMPLE.get(type(opts[0]), "string") if opts else "string"
        return {"type": kind, "enum": opts}
    if origin in (list, typing.List):           # noqa: UP006
        args = get_args(ann)
        return {"type": "array",
                "items": _json_type(args[0]) if args else {"type": "string"}}
    if origin in (dict, typing.Dict):           # noqa: UP006
        return {"type": "object"}
    if origin in (typing.Union, types.UnionType):
        inner = [a for a in get_args(ann) if a is not type(None)]
        if len(inner) == 1:
            return _json_type(inner[0])
        return {}                               # genuinely any
    if typing.is_typeddict(ann):
        props: dict[str, Any] = {}
        # Required[...] read from the hint too: under `from __future__ import
        # annotations` __required_keys__ does not see it
        required = set(ann.__required_keys__)
        for key, hint in typing.get_type_hints(ann, include_extras=True).items():
            if get_origin(hint) is typing.Required:
                required.add(key)
            elif get_origin(hint) is typing.NotRequired:
                required.discard(key)
            if get_origin(hint) in (typing.Required, typing.NotRequired):
                hint = get_args(hint)[0]
            desc = None
            if get_origin(hint) is Annotated:
                hint, *extra = get_args(hint)
                desc = next((a for a in extra if isinstance(a, str)), None)
            props[key] = _json_type(hint) | ({"description": desc} if desc else {})
        return {"type": "object", "properties": props,
                "required": sorted(required), "additionalProperties": False}
    return {"type": "string"}


def derive_schema(fn: Callable) -> dict[str, Any]:
    """Signature -> JSON Schema. ToolContext is removed; the dispatcher injects it. A
    parameter named like a Python keyword ends in an underscore in the coroutine
    (`in_`) and loses it on the wire (`in`); turns/tools._run_call maps it back."""
    hints = typing.get_type_hints(fn, include_extras=True)
    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in inspect.signature(fn).parameters.items():
        if pname in ("self", "ctx"):
            continue
        ann = hints.get(pname, str)
        if pname.endswith("_") and keyword.iskeyword(pname[:-1]):
            pname = pname[:-1]
        desc = None
        if get_origin(ann) is Annotated:
            args = get_args(ann)
            ann = args[0]
            desc = next((a for a in args[1:] if isinstance(a, str)), None)
        optional = param.default is not inspect.Parameter.empty
        if get_origin(ann) in (typing.Union, types.UnionType) and \
                type(None) in get_args(ann):
            optional = True
        spec = _json_type(ann)
        if desc:
            spec["description"] = desc
        props[pname] = spec
        if not optional:
            required.append(pname)
    # additionalProperties false: an invented parameter gets a readable error
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def collect(skill: Any) -> list[ToolSpec]:
    out: list[ToolSpec] = []
    for name in dir(skill):
        if name.startswith("_"):
            continue
        fn = getattr(skill, name)
        meta = getattr(fn, "__celine_tool__", None)
        if meta is None:
            continue
        doc = inspect.getdoc(fn) or ""
        if name.endswith("_"):      # continue_ is continue, status_ is status (the skill's
            name = name[:-1]        # own `status` says whether it is up)
        out.append(ToolSpec(skill=skill.name, name=name, description=doc.strip(),
                            schema=derive_schema(fn), fn=fn, **meta))
    return sorted(out, key=lambda t: t.name)


@dataclass
class Registry:
    cfg: Any = None     # set: tool descriptions name the user ({{username}})
    skills: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, ToolSpec] = field(default_factory=dict)
    app: Any = None     # the running app (core/services.py); None in a bare test registry

    def add(self, skill: Any) -> None:
        self.add_specs(skill, collect(skill))

    def add_specs(self, skill: Any, specs: list[ToolSpec]) -> None:
        """Replace one skill's tools wholesale. The dict is rebuilt, never mutated
        under a turn that is iterating it."""
        self.skills[skill.name] = skill
        kept = {k: v for k, v in self.tools.items() if v.skill != skill.name}
        for spec in specs:
            if self.cfg is not None and not features.tool_on(self.cfg, spec.wire_name):
                continue                # its feature is off (shell__share: shares)
            if self.cfg is not None:
                # prompt text, filled in once here: {{username}} and {{name}}
                spec = replace(spec, description=self.cfg.substitute(spec.description),
                               schema=_filled(spec.schema, self.cfg))
            kept[spec.wire_name] = spec
        self.tools = kept

    def remove(self, name: str) -> None:
        self.skills.pop(name, None)
        self.tools = {k: v for k, v in self.tools.items() if v.skill != name}

    def get(self, wire_name: str) -> ToolSpec | None:
        return self.tools.get(wire_name)

    def down(self, skill_name: str) -> str | None:
        """Why a skill is not up (a connector that is starting or dead, image generation
        without a preset), or None while it is up. Its tools stay in its list as stubs
        that say so."""
        skill = self.skills.get(skill_name)
        status = getattr(skill, "status", "up")
        if status == "up":
            return None
        return " ".join(str(getattr(skill, "error", None) or f"it is {status}").split())[:200]

    def index(self, cfg, carried) -> str | None:
        """The skill index for the system prompt (turns/loads.py decides `carried`): the
        loaded skills by name, then one line per skill that is not loaded, what it is
        for."""
        from .meta import skill_note_path
        from ..turns.loads import shares
        idle = int(shares(cfg)["idle_turns"])

        def line(name: str) -> str:
            desc = " ".join(cfg.substitute(str(getattr(self.skills[name], "description",
                                                       ""))).split())
            if why := self.down(name):
                desc += f" (down: {cfg.substitute(why)})"
            elif carried.others.get(name) == "off":
                desc += " (off in Settings, Tools: it cannot load)"
            elif carried.others.get(name) == "off here":
                desc += " (off in this conversation: it cannot load)"
            mark = " [notes]" if skill_note_path(cfg, name).is_file() else ""
            return f"- {name}{mark}: {desc}"
        loaded = [n for n in carried.skills if n != META]
        others = [line(n) for n in self.skills if n in carried.others]
        if not loaded and not others:
            return None
        if META in carried.skills:
            head = (f"Your skills. Each tool's name is `<skill>__<verb>`. The tools of the "
                    f"loaded skills are in your list; a tool listed with no parameters "
                    f"gets them when you load it or call it. `skills__load` loads skills "
                    f"and tools for your next step, `skills__unload` lets them go; a skill "
                    f"or tool you do not use for {idle} turns unloads. "
                    f"`skills__read_guide` shows a skill's guide, with examples, and your "
                    f"notes on a skill marked [notes] (skills/<name>.md).")
        else:       # its tools leave out the skills skill: they load by need only
            head = (f"Your skills. Each tool's name is `<skill>__<verb>`. The tools of the "
                    f"loaded skills are in your list; a tool listed with no parameters "
                    f"gets them when you call it. Another skill loads when the talk needs "
                    f"it, and a skill or tool you do not use for {idle} turns unloads.")
        out = [head, f"Loaded: {', '.join(loaded)}."]
        if others:
            out += ["Not loaded:", *others]
        return "\n".join(out)

    def by_skill(self) -> dict[str, list[ToolSpec]]:
        out: dict[str, list[ToolSpec]] = {s: [] for s in self.skills}
        for spec in self.tools.values():
            out.setdefault(spec.skill, []).append(spec)
        return out

    async def prompt_fragments(self, ctx: ToolContext, names) -> list[str]:
        """What the skills named in `names` add to the system prompt every turn."""
        out = []
        for skill in self.skills.values():
            frag = getattr(skill, "prompt_fragment", None) if skill.name in names else None
            if frag is None:
                continue
            try:
                text = await frag(ctx)
            except Exception as e:
                log.exception("prompt fragment failed",
                              extra={"event": "skill.fragment_failed",
                                     "skill": skill.name})
                text = f"The {skill.name} skill's part of this prompt failed: {e}"
            if text:
                out.append(text)
        return out

    async def message_context(self, ctx: ToolContext, message: dict
                              ) -> tuple[str, dict] | None:
        """What skills add to the end of one user message, asked once when it is first
        sent (turns/message_context.py). Each skill's `message_context(ctx, message)`
        returns (text, report) or None; a report with no text is kept too. -> the texts a
        blank line apart and the reports by skill, or None when no skill adds anything."""
        texts, report = [], {}
        for skill in self.skills.values():
            hook = getattr(skill, "message_context", None)
            if hook is None:
                continue
            try:
                got = await hook(ctx, message)
            except Exception as e:
                log.exception("message context failed",
                              extra={"event": "skill.message_context_failed",
                                     "skill": skill.name})
                got = (f"The {skill.name} skill's context for this message failed: {e}",
                       {"error": str(e)})
            if got and (got[0] or got[1]):     # a report alone: what it decided
                if got[0]:
                    texts.append(got[0])
                report[skill.name] = got[1]
        return ("\n\n".join(texts), report) if texts or report else None


# every built-in skill but `skills` (it reads the live registry and is added last), in
# the order they are listed: (name, module relative to this package, class). `build`, the
# config reload and the knobs (knob_fields) all go through this one list, and each skips a
# skill whose module is gone: removing a skill's module or folder removes the skill.
# config/skills.toml has one table per built-in skill, `enabled` and nothing else.
BUILTIN = (
    ("notebook", "notebook", "Notebook"),
    ("mind", "mind", "Mind"),
    ("shell", "shell", "Shell"),
    ("code", "code", "Code"),
    ("past_conversations", "conversations", "PastConversations"),
    ("intercom", ".intercom.skill", "IntercomSkill"),
    ("missions", ".missions.skill", "Missions"),
    ("plan", ".plans.skill", "Plan"),
    ("routines", ".schedules.skill", "Routines"),
    ("sub_agents", ".sub_agents.skill", "SubAgents"),
    ("chat_archive", "archive", "ChatArchive"),
    ("user_documents", "documents", "UserDocuments"),
    ("web", "web", "Web"),
    ("email", "mail", "Email"),
    ("browser", "browser", "Browser"),
    ("vault", ".vault.skill", "VaultSkill"),
    ("artifacts", "artifacts", "Artifacts"),
    ("image_generator", "images", "ImageGenerator"),
    ("music_generator", "music", "MusicGenerator"),
    ("writer", "writer", "Writer"),
    ("models", "models", "Models"),
    ("appearance", "appearance", "Appearance"),
    ("voice", "voice", "Voice"),
    ("me", "me", "Me"),
    ("bugreport", ".bugreports.skill", "BugReport"),
)

# What it carries by need (turns/loads.py). The core skills are loaded wherever they
# are offered; every other skill loads when it asks for it (skills__load), uses it, or
# a new user message says a word of its guide's `words:` line.
CORE = {META, "notebook", "intercom", "sub_agents", "past_conversations",
        "missions", "mind", "plan", "bugreport"}


def build(app) -> Registry:
    """Only enabled skills are constructed. A skill that is off globally is not. Every
    skill gets the running app as `self.app` (core/services.py) and reads what it needs
    from it when a tool runs."""
    from .meta import Skills

    cfg = app.cfg
    reg = Registry(cfg, app=app)
    for name, mod_name, cls_name in BUILTIN:
        if not cfg.skill_enabled(name) or (mod := module(mod_name)) is None:
            continue
        cls = getattr(mod, cls_name)
        skill = cls(cfg)
        skill.app = app
        reg.add(skill)
    for p in plugins.all():          # imported only while its switch is on
        try:
            built = []
            for name, spec in p.skills.items():
                if cfg.skill_enabled(name):
                    skill = p.attr(spec["class"])(cfg, p)
                    if (g := getattr(type(skill), "guide", None)) is not None:
                        g._parse()          # a missing or misnamed guide.md
                    skill.app = app
                    built.append(skill)
        except Exception as e:      # a broken plugin: its skills stay out, the app runs
            log.exception("plugin skills failed", extra={"event": "plugin.build_failed",
                                                         "plugin": p.id})
            problem = Problem(f"plugin {p.id}", f"{type(e).__name__}: {e}")
            if problem not in cfg.problems:
                cfg.problems.append(problem)
            continue
        for skill in built:
            reg.add(skill)
    meta = Skills(reg, cfg)
    meta.app = app
    reg.add(meta)
    return reg


def names() -> list[str]:
    """Every skill `build` may make but `skills`: BUILTIN's, then the plugins'."""
    return [n for n, _, _ in BUILTIN] + [s for p in plugins.all() for s in p.skills]


def module(name: str) -> types.ModuleType | None:
    """A BUILTIN skill's module (`name` relative to this package), None when its file or
    its folder is gone. Any other import error is raised: a broken skill refuses the
    start."""
    import importlib
    import importlib.util
    full = importlib.util.resolve_name(f".{name}", __name__)
    try:
        return importlib.import_module(full)
    except ModuleNotFoundError as e:
        if e.name and (full == e.name or full.startswith(e.name + ".")):
            return None
        raise


def knob(key: str, label: str, unit: str | None, lo: float, hi: float, default: float,
         help: str | None = None) -> dict[str, Any]:
    """One number a skill reads at use, `cfg.r(key)`: declared in its module's `KNOBS`
    (key `<skill>.<name>`, the default the constant), shown on Settings, Advanced under
    the skill's label, kept in var/settings/general.toml in the skill's table. A float
    default takes any number, an int one whole numbers."""
    return {"key": key, "label": label, "type": "number", "unit": unit, "min": lo,
            "max": hi, "default": default, **({"help": help} if help else {})}


def knob_fields() -> list[tuple[str, dict[str, Any]]]:
    """(skill, field) for every knob of every BUILTIN skill whose module is there."""
    out = []
    for name, mod_name, _ in BUILTIN:
        mod = module(mod_name)
        for f in getattr(mod, "KNOBS", ()) if mod is not None else ():
            if not f["key"].startswith(name + "."):
                raise ValueError(f"{mod_name}: knob {f['key']} is not {name}.<name>")
            out.append((name, f))
    return out
