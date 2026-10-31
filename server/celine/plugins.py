"""Plugins: folders that add to the core. Base plugins ship in the repo (`plugins/<id>/`),
user plugins live in `<data>/plugins/<id>/`; a user plugin replaces the base plugin with
the same id.

A plugin is a folder with a `plugin.toml` (its feature and its skills), and optionally a
`settings.toml` (its form, settings/form.py), `migrations/` (its own SQLite file,
`<data>/plugins/<id>/<id>.db`) and Python modules imported as `celine_plugins.<id>.<name>`.

`scan` reads only TOML. A plugin's Python is imported only while its switch is on: by
`skills.build` (its skills) and by its runner (api/plugins.py: its file, its routes, its
`run`). Off at runtime stops the runner, removes its skills and routes and closes its
file; Python cannot unload a module, so its code stays in memory until the next start.
A plugin added to or removed from disk needs a restart.
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import sys
import tomllib
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import ROOT
from .errors import Busy

BASE = ROOT / "plugins"
PACKAGE = "celine_plugins"
_ID = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class Plugin:
    id: str
    dir: Path                       # its code
    user: bool                      # from the data dir
    manifest: dict[str, Any]        # plugin.toml
    form: dict[str, Any] | None     # settings.toml, None without one
    file: Any = None                # its KBFile while on
    data: Path | None = None        # <data>/plugins/<id>/ while on

    def module(self, name: str) -> types.ModuleType:
        """Its module `name` (relative to its folder), imported on first use."""
        pkg = f"{PACKAGE}.{self.id}"
        if pkg not in sys.modules:
            if PACKAGE not in sys.modules:
                parent = types.ModuleType(PACKAGE)
                parent.__path__ = []
                sys.modules[PACKAGE] = parent
            spec = importlib.util.spec_from_file_location(
                pkg, self.dir / "__init__.py", submodule_search_locations=[str(self.dir)])
            mod = importlib.util.module_from_spec(spec)
            sys.modules[pkg] = mod
            try:
                spec.loader.exec_module(mod)
            except BaseException:
                del sys.modules[pkg]
                raise
        return importlib.import_module(f"{pkg}.{name}")

    def attr(self, ref: str) -> Any:
        """"module:attr" of its folder."""
        mod, _, name = ref.partition(":")
        return getattr(self.module(mod), name)

    @property
    def db(self) -> Any:
        """Its file while it runs; between a switch and its runner, or after a runner
        that failed, a CelineError that says so (a tool or a route answers with it)."""
        if self.file is None:
            raise Busy(f"{self.manifest['label']} is not running.")
        return self.file

    @property
    def skills(self) -> dict[str, dict[str, Any]]:
        return self.manifest.get("skills", {})

    @property
    def migrations(self) -> Path | None:
        d = self.dir / "migrations"
        return d if d.is_dir() else None


LOADED: dict[str, Plugin] = {}
_scanned: Path | None = None        # the data dir of the last scan


def scan(data_dir: Path | None, reserved: frozenset[str] = frozenset(),
         check_form: Callable[[str, Any], None] | None = None) -> list[str]:
    """Read every manifest, base then user (same id replaces), into LOADED. `reserved`:
    the ids a plugin may not take (core features, settings sections); `check_form`
    raises ValueError on a settings.toml (settings/form.py check_toml). Again for the same
    data dir (a reload) only the manifests and forms of the plugins already known are read
    again: the running ones keep their records. -> problems, one sentence each."""
    global _scanned
    problems: list[str] = []
    found: dict[str, Plugin] = {}
    roots = [(BASE, False)] + ([(Path(data_dir) / "plugins", True)] if data_dir else [])
    for root, user in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not (d / "plugin.toml").is_file():
                continue
            try:
                p = _read(d, user, reserved, check_form)
            except (ValueError, tomllib.TOMLDecodeError, OSError) as e:
                problems.append(f"plugins/{d.name}: {e}")
                continue
            found[p.id] = p
    if data_dir is not None and _scanned == Path(data_dir):
        if {(p.id, p.dir) for p in found.values()} != {(p.id, p.dir) for p in LOADED.values()}:
            problems.append("plugins changed on disk; restart Celine to apply it")
        for pid, p in LOADED.items():
            if (new := found.get(pid)) is not None and new.dir == p.dir:
                p.manifest, p.form = new.manifest, new.form
        return problems
    LOADED.clear()
    LOADED.update(found)
    _scanned = Path(data_dir) if data_dir is not None else None
    return problems


def _read(d: Path, user: bool, reserved: frozenset[str],
          check_form: Callable[[str, Any], None] | None) -> Plugin:
    with (d / "plugin.toml").open("rb") as f:
        manifest = tomllib.load(f)
    pid = manifest.get("id")
    if pid != d.name:
        raise ValueError(f"its id must be its folder's name, {d.name!r}")
    if not _ID.match(pid) or pid in reserved:
        raise ValueError(f"{pid!r} cannot be a plugin id")
    if not isinstance(manifest.get("label"), str) or not manifest["label"]:
        raise ValueError("it needs a label")
    form = None
    if (d / "settings.toml").is_file():
        with (d / "settings.toml").open("rb") as f:
            form = tomllib.load(f)
        if not isinstance(form.get("fields"), list):
            raise ValueError("settings.toml needs [[fields]]")
        if check_form is not None:
            check_form(pid, form)
    return Plugin(pid, d, user, manifest, form)


def all() -> list[Plugin]:      # noqa: A001 (plugins.all())
    return list(LOADED.values())


def get(pid: str) -> Plugin | None:
    return LOADED.get(pid)


def with_form() -> list[Plugin]:
    return [p for p in LOADED.values() if p.form is not None]


ROUTES = "/api/plugins/"


def is_open(method: str, path: str) -> bool:
    """A route of a running plugin that its manifest's `open` ("POST /gateway") lets a
    machine reach without a session (auth/gate.py); the route checks its own key."""
    if not path.startswith(ROUTES):
        return False
    pid, _, rest = path[len(ROUTES):].partition("/")
    p = LOADED.get(pid)
    return p is not None and p.data is not None and any(
        _matches(f"{method} /{rest}", o) for o in p.manifest.get("open", ()))


def _matches(route: str, pattern: str) -> bool:
    """"GET /setup/abc" against "GET /setup/{token}": a {name} is one nonempty segment."""
    a, b = route.split("/"), pattern.split("/")
    return len(a) == len(b) and not any(
        x != y and not (y.startswith("{") and y.endswith("}") and x) for x, y in zip(a, b))
