"""config/celine.toml into a Config, and the helpers every config file is read with."""
from __future__ import annotations

import functools
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ["CELINE_CONFIG_DIR"]).expanduser() if os.environ.get(
    "CELINE_CONFIG_DIR") else ROOT / "config"
ENV_FILE = ROOT / ".env"

# CELINE_STATIC_DIR: serve a web build from elsewhere
STATIC_DIR = Path(os.environ.get("CELINE_STATIC_DIR") or Path(__file__).parent / "static")

# where the code is (the AGPL's offer of it to everyone who uses the app); a fork names its own
REPOSITORY = "https://github.com/LumiWasTaken/celine-net"


@functools.cache
def build() -> dict:
    """Which build this is, for the rail's brand mark and Settings, General (GET
    /api/status `build`): the image's CELINE_COMMIT and CELINE_BUILT (Dockerfile, its last
    layer), else git's commit in a checkout and the time of the web build in STATIC_DIR;
    `source`, its code: REPOSITORY at that commit when there is one."""
    commit = os.environ.get("CELINE_COMMIT")
    if not commit:
        try:
            commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                    cwd=Path(__file__).parent, capture_output=True,
                                    text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    built = os.environ.get("CELINE_BUILT") or None
    if not built and (STATIC_DIR / "index.html").exists():
        built = datetime.fromtimestamp((STATIC_DIR / "index.html").stat().st_mtime, UTC).isoformat()
    commit = commit or "dev"
    known = re.fullmatch(r"[0-9a-f]{7,40}", commit)
    return {"commit": commit, "built": built,
            "source": f"{REPOSITORY}/tree/{commit}" if known else REPOSITORY}


def load_env(path: Path = ENV_FILE) -> list[str]:
    """KEY=VALUE lines into the environment. Existing variables win. Returns the
    names set, so a log line can say what was loaded without saying the values."""
    if not path.is_file():
        return []
    names: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            names.append(key)
    return names


class ConfigError(Exception):
    """Raised only for problems that make startup impossible."""


@dataclass(frozen=True)
class Problem:
    """Something wrong with the configuration that is not worth dying over."""

    file: str
    message: str
    fatal: bool = False

    def __str__(self) -> str:
        return f"{self.file}: {self.message}"


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()


def read_toml(path: Path, problems: list[Problem]) -> dict[str, Any]:
    """Parse one TOML file. A missing file is {} (its defaults); one that does not parse
    is a ConfigError (celine exits 2 with the file, line and column: a stranger must
    see it, not a default that quietly runs)."""
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{shown_path(path)}: {e}") from None
    except OSError as e:
        problems.append(Problem(shown_path(path), f"could not be read: {e.strerror}"))
        return {}


def shown_path(path: Path) -> str:
    """A path as a problem names it: relative to the repo when it is inside it."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


# The keys a config file may hold and the type its reader expects: int, num (int or
# float), str, bool, list (of text), dict, or a tuple of the allowed values. An unknown
# key or a wrong type is a Problem (Settings, General; celine doctor) and the key is
# dropped, so the reader's default counts. Each owner keeps the table of its own file
# (mcp.py, kb/__init__.py, skills/__init__.py, backends/llm.py); boot.py reads them.
SERVER_KEYS = {             # __main__.py, auth/__init__.py, api/origin.py
    "host": "str", "port": "int", "login": "str",
    "allowed_hosts": "list", "trusted_proxies": "list", "health_port": "int",
    "metrics_port": "int",
}
_TYPES = {"int": "a whole number", "num": "a number", "str": "text", "bool": "true or false",
          "list": "a list", "dict": "a table"}

# environment overrides for [server], so a container can bind 0.0.0.0 and turn the login
# on while config/ stays a read-only copy of the files you run locally
_SERVER_ENV = {"CELINE_HOST": "host", "CELINE_PORT": "port", "CELINE_LOGIN": "login",
               "CELINE_TRUSTED_PROXIES": "trusted_proxies",
               "CELINE_HEALTH_PORT": "health_port",
               "CELINE_METRICS_PORT": "metrics_port"}


@dataclass
class BackendConfig:
    """One model on one provider, built from var/settings by
    celine.settings.registry.backend_config: what backends/openai.py takes. `raw`
    carries the kind-specific request extras (llama sampler block, OpenRouter routing)
    and provider facts."""

    id: str
    kind: str
    label: str
    base_url: str
    model: str
    params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    _api_key: str | None = None

    @property
    def api_key(self) -> str | None:
        return self._api_key


@dataclass
class Config:
    """config.load fills `server` and `data_dir`; boot.load the rest."""

    server: dict[str, Any]
    data_dir: Path
    problems: list[Problem] = field(default_factory=list)
    name: str = ""             # its own (celine.her)
    user: str = ""             # yours (Settings, General)
    skills: dict[str, dict[str, Any]] = field(default_factory=dict)
    kb: dict[str, dict[str, Any]] = field(default_factory=dict)
    embeddings: dict[str, Any] = field(default_factory=dict)
    mcp: dict[str, Any] = field(default_factory=dict)    # id -> mcp.McpServerConfig
    # every MCP server's rows, mcp.toml's and the plugins' (mcp.row_words, skills/words.py)
    tool_words: dict[str, Any] = field(default_factory=dict)
    llm: dict[str, Any] = field(default_factory=dict)   # config/llm.toml: [queue]
    media: dict[str, Any] = field(default_factory=dict)  # kb.toml [media]: kb/media.py
    # config/image_presets and music_presets, as read (comfyui/presets.py `load`): kept
    # here so that a changed preset file counts as a configuration change (reload.py)
    presets: dict[str, Any] = field(default_factory=dict)
    # var/settings/*.toml (celine.settings.registry.Settings): providers, model roles,
    # tools, general (with the Advanced values: `r`)
    settings: Any = None
    # its row (celine.her.Her); None before the first start
    her: Any = None
    # var/settings/schedules (celine.schedules): name -> Schedule
    schedules: dict[str, Any] = field(default_factory=dict)
    bio: str = ""              # what the user wrote about themselves (General settings)
    timezone: str = "UTC"      # the app's time zone (General settings, celine/clock.py)
    pronouns: str = "she"      # its own: {{she}}, {{her}}, {{herself}}, {{them}}
    # var/settings/context.toml (turns/loads.py): the shares of the window it carries
    # notes and tools in, and the turns a loaded one stays without being touched
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return str(self.server["host"])

    @property
    def port(self) -> int:
        return int(self.server["port"])

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "celine.db"

    def r(self, key: str) -> Any:
        """An Advanced setting as saved now, else its default (settings/general.py)."""
        return self.settings.value(key)

    def skill(self, skill_id: str) -> dict[str, Any]:
        return self.skills.get(skill_id, {})

    def skill_enabled(self, skill_id: str) -> bool:
        """config/skills.toml `enabled` (true when the block says nothing), and False
        while the feature that owns the skill is off (settings/features.py, through
        Settings.skills_off)."""
        if skill_id in self.settings.skills_off:
            return False
        return bool(self.skill(skill_id).get("enabled", True))

    def substitute(self, text: str, *, braces: bool = False, agent: str | None = None) -> str:
        """{{username}} or {{user}} (the user's name) and {{name}} or {{char}} (its own) in
        prompt text, and its pronouns: {{she}}, {{her}} (possessive), {{herself}},
        {{them}} (the object: it, him, them), capitalised as written ({{Her}}). Resolved
        when a request is built, never stored resolved; never applied to tool output,
        where the same strings may be real content. `braces`: a template that goes to
        str.format next, so a { or } in a name is doubled and stays as written."""
        her = getattr(self, "her", None)
        name = her.name if her is not None else self.name
        pronouns = her.pronouns if her is not None else self.pronouns
        for key, value in (("{{username}}", self.user), ("{{user}}", self.user),
                           ("{{agent_name}}", agent or name), ("{{name}}", name), ("{{char}}", name)):
            text = text.replace(key, value.replace("{", "{{").replace("}", "}}")
                                if braces else value)
        if "{{" not in text:
            return text
        words = PRONOUNS.get(pronouns, PRONOUNS["she"])
        for key, word in zip(("she", "her", "herself", "them"), words):
            text = text.replace("{{" + key + "}}", word)
            text = text.replace("{{" + key.capitalize() + "}}", word.capitalize())
        return text

    def secrets(self) -> list[str]:
        """Every secret value currently in play, for redact(). Rebuilt on reload."""
        out = [str(v) for s in self.mcp.values() for v in s.headers.values() if len(str(v)) > 8]
        out.extend(self.settings.typed_keys())      # keys typed into Settings
        return [s for s in dict.fromkeys(out) if s]


# it, it (possessive), itself, them (the object) for each choice of a character's
# pronouns
PRONOUNS = {"she": ("she", "her", "herself", "her"), "he": ("he", "his", "himself", "him"),
            "they": ("they", "their", "themselves", "them")}


def _is(v: Any, t: Any) -> bool:
    if isinstance(t, tuple):
        return v in t
    if t == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "num":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, {"str": str, "bool": bool, "list": list, "dict": dict}[t])


def _want(t: Any) -> str:
    return "one of " + ", ".join(t) if isinstance(t, tuple) else _TYPES[t]


def checked(file: str, section: str, got: Any, known: dict[str, Any],
            problems: list[Problem]) -> dict[str, Any]:
    """`got` with every unknown key and every wrongly typed key dropped, each one a
    Problem that names the file, the key and the type expected."""
    where = f"[{section}] " if section else ""
    if not isinstance(got, dict):
        problems.append(Problem(file, f"{where.strip() or file} is not a table, ignored"))
        return {}
    out: dict[str, Any] = {}
    for k, v in got.items():
        if k not in known:
            problems.append(Problem(file, f"unknown key {where}{k}, ignored"))
        elif not _is(v, known[k]):
            problems.append(Problem(file, f"{where}{k} must be {_want(known[k])}, not "
                                          f"{type(v).__name__}; the default is used"))
        else:
            out[k] = v
    return out


def tables(file: str, got: dict[str, Any], known: dict[str, dict[str, Any]],
           problems: list[Problem]) -> dict[str, dict[str, Any]]:
    """A file of tables: each known table checked, an unknown one a Problem."""
    out: dict[str, dict[str, Any]] = {}
    for name, table in got.items():
        if name not in known:
            problems.append(Problem(file, f"unknown table [{name}], ignored"))
        else:
            out[name] = checked(file, name, table, known[name], problems)
    return out


def _from_env(env: str, v: str, t: str) -> Any:
    if t == "int":
        try:
            return int(v)
        except ValueError:
            raise ConfigError(f"{env}={v!r} is not a whole number") from None
    if t == "list":
        return [p.strip() for p in v.split(",") if p.strip()]
    return v


def load() -> Config:
    """config/celine.toml and the environment: how the app is served and where its data
    lives. Reads and never writes: startup makes the data dir and the keys
    (__main__.preflight). The whole configuration is boot.load's."""
    load_env()
    problems: list[Problem] = []
    path = CONFIG_DIR / "celine.toml"
    if not path.exists():
        raise ConfigError(f"no config/celine.toml under {ROOT}")
    main = tables(shown_path(path), read_toml(path, problems),
                  {"server": SERVER_KEYS, "paths": {"data": "str"}}, problems)
    server = {"host": "127.0.0.1", "port": 8766, **main.get("server", {})}
    for env, key in _SERVER_ENV.items():
        if os.environ.get(env):
            server[key] = _from_env(env, os.environ[env], SERVER_KEYS[key])
    data_dir = Path(main.get("paths", {}).get("data", "var"))
    if os.environ.get("CELINE_DATA_DIR"):
        data_dir = _expand(os.environ["CELINE_DATA_DIR"])     # a second instance beside the real one
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    return Config(server=server, data_dir=data_dir, problems=problems)
