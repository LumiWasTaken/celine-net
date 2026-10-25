"""The settings that start as a copy of a shipped default: the context shares and the
schedules. config/ ships the defaults; startup (__main__.preflight) copies each into
var/settings/ when its place there does not exist (a deleted folder comes back), and from
then on only var/settings is read: a schedule shipped later reaches an install that has
the folder only by hand or on the Routines page. The agent's own row is celine.her.

- `var/settings/context.toml`: config/context.toml, the shares of the window it
  carries notes in, and the tokens of tools (turns/loads.py).
- `var/settings/schedules/`: config/schedules/*.toml and the partner prompts *.md
  (celine/schedules).
Bastet's persona and voice are never seeded: config/guard/ is their default (guard/persona.py).
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..config import CONFIG_DIR
from .files import write_bytes


def schedules_dir(root: Path) -> Path:
    return root / "schedules"


def _copy(src: Path, dst: Path, patterns: tuple[str, ...]) -> None:
    """The folder's files matching `patterns` into `dst`, which is made only once they
    are all copied, so a crash halfway copies again on the next start."""
    tmp = dst.with_name(f".{dst.name}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for pattern in patterns:
        for p in src.glob(pattern):
            shutil.copyfile(p, tmp / p.name)
    tmp.rename(dst)


def context_path(root: Path) -> Path:
    return root / "context.toml"


def seed(root: Path) -> None:
    """Copies what does not exist yet; never touches what does."""
    root.mkdir(parents=True, exist_ok=True)
    if not context_path(root).exists():
        write_bytes(context_path(root), (CONFIG_DIR / "context.toml").read_bytes())
    if not schedules_dir(root).exists():
        _copy(CONFIG_DIR / "schedules", schedules_dir(root), ("*.toml", "*.md"))



# config/skills.toml kept a skill's numbers in its table until they moved to Settings,
# Advanced (var/settings/general.toml, a table per skill). The old key -> the new one and
# how its value converts; a key that kept its name maps to itself.
_MOVED = {
    "notebook.index_max_bytes": ("notebook.index_max_chars", int),
    "web.max_bytes": ("web.page_max_mb", lambda v: max(1, round(v / 2**20))),
    "web.pdf_max_bytes": ("web.pdf_max_mb", lambda v: max(1, round(v / 2**20))),
    "chat_archive.verify_citations": ("verify_citations", bool),
}


def skill_knobs(root: Path, skills_toml: Path) -> list[str]:
    """The numbers an older config/skills.toml sets, into general.toml where general.toml
    has none for them yet; never overwrites a saved one. The keys moved."""
    import tomllib

    from . import files, general
    try:
        old = tomllib.loads(skills_toml.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    saved, err = files.read(root, "general")
    if err:
        return []
    saved = saved or {}
    have = general.validate(saved)
    known = general.schema()[1]
    moved: dict[str, object] = {}
    for skill, table in old.items():
        for name, v in (table.items() if isinstance(table, dict) else ()):
            if name == "enabled":
                continue
            key, conv = _MOVED.get(f"{skill}.{name}", (f"{skill}.{name}", lambda x: x))
            if key not in known or key in have:
                continue
            try:
                moved.update(general.validate({key: conv(v)}))
            except (files.Invalid, TypeError, ValueError):
                continue
    if moved:
        from . import section_to_file
        files.write(root, "general", section_to_file("general", {**have, **moved}))
    return sorted(moved)


def speech_roles(root: Path, secrets_root: Path) -> list[str]:
    """var/settings/speech.toml (the speech providers before they were providers) into
    providers.toml and the Dictation and Read aloud roles: each address becomes a provider
    (a saved one with the same address is used), its key that provider's key, its model
    that role's. speech.toml and its keys go once the new files are written, so a crash
    halfway runs it again. -> the roles it set."""
    import tomllib

    from . import file_to_section, files, kinds, registry, section_to_file, secrets
    path = files.path_for(root, "providers").with_name("speech.toml")
    try:
        old = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    raw, err = files.read(root, "providers")
    roles, rerr = files.read(root, "roles")
    if err or rerr:
        return []
    providers = file_to_section("providers", raw or {})
    roles = roles or {}
    done = []
    for direction, role, cap, name, label in (
            ("input", "transcription", "transcription", "dictation", "Dictation"),
            ("output", "speech", "speech", "read-aloud", "Read aloud")):
        url, model = (old.get(direction) or {}).get("url"), (old.get(direction) or {}).get("model")
        if not url or not model:
            continue
        url = kinds.normalize_url("openai", url)
        pid = next((k for k, p in providers.items()
                    if p.get("base_url", "").rstrip("/") == url.rstrip("/")), None)
        if pid is None:
            pid = next(f"{name}{n or ''}" for n in range(100) if f"{name}{n or ''}" not in providers)
            providers[pid] = {"kind": "openai", "label": label,
                              "base_url": url, "models": {}}
            key = secrets.read_key(secrets_root, f"speech-{direction}")
            if key:
                secrets.set_key(secrets_root, pid, key)
        m = providers[pid].setdefault("models", {}).setdefault(model, {"label": model, "caps": []})
        m["caps"] = list(dict.fromkeys([*(m.get("caps") or []), cap]))
        if not (roles.get(role) or {}).get("chain"):
            roles[role] = {"chain": [f"{pid}/{model}"]}
            done.append(role)
    try:
        clean = registry.validate_providers(providers)
        roles = registry.validate_roles(roles, clean)
    except files.Invalid:
        return []                     # a file that does not load: Settings says why
    files.write(root, "providers", section_to_file("providers", clean))
    files.write(root, "roles", section_to_file("roles", roles))
    path.unlink()
    for direction in ("input", "output"):
        secrets.delete_key(secrets_root, f"speech-{direction}")
    return done
