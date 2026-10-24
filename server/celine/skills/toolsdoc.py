"""A skill's full reference: its guide (guides/<skill>.md) plus every tool with its
arguments, rendered from the registry. The notebook browser shows it to you under
tools/ (generated, read-only)."""
from __future__ import annotations

import json

from . import guide_text


def _kind(p: dict) -> str:
    if "enum" in p:
        return " | ".join(json.dumps(v) for v in p["enum"])
    t = p.get("type", "any")
    if t == "array":
        return f"list of {_kind(p.get('items') or {})}"
    return str(t)


def tool_reference(spec) -> list[str]:
    lines = [f"## {spec.wire_name}", "", spec.description.strip() or "(no description)", ""]
    a = spec.annotations
    effects = "read-only" if a.read_only else (
        "changes data" + (", may overwrite or delete" if a.destructive else ""))
    if a.open_world:
        effects += ", reaches outside the app"
    lines.append(f"effects: {effects}")
    if spec.background:
        lines.append(f"{spec.background}=true hands it to a background job; its result "
                     f"arrives later as a message.")
    props = spec.schema.get("properties") or {}
    required = set(spec.schema.get("required") or [])
    if props:
        lines.append("arguments:")
        for pname, pspec in props.items():
            need = "required" if pname in required else "optional"
            desc = pspec.get("description", "")
            lines.append(f"- `{pname}` ({_kind(pspec)}, {need}){': ' + desc if desc else ''}")
    else:
        lines.append("arguments: none")
    lines.append("")
    return lines


def reference(registry, skill_name: str, cfg) -> str:
    """The guide and every tool of the skill."""
    skill = registry.skills.get(skill_name)
    g = getattr(skill, "guide", None)
    lines = [f"# {skill_name}", ""]
    lines += [guide_text(cfg, g.body), ""] if g is not None \
        else [str(getattr(skill, "description", "")), ""]
    status = getattr(skill, "status", None)
    if status and status != "up":
        lines += [f"status: {status}", ""]
    for spec in sorted(registry.by_skill().get(skill_name, []), key=lambda s: s.name):
        lines += tool_reference(spec)
    for fname, text in (getattr(skill, "docs", None) or {}).items():
        lines += [f"---- {fname} ----", text.strip(), ""]
    return cfg.substitute("\n".join(lines).strip())


def render(registry, cfg) -> dict[str, str]:
    """{path: content} for the read-only tools/ folder of the notebook browser."""
    return {f"tools/{name}.md": reference(registry, name, cfg)
            for name in sorted(registry.skills)}
