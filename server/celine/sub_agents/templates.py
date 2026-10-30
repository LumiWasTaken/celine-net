"""Her agent templates: the sub-agents she reuses, one note each in FOLDER of her notebook,
in the shape of Claude Code's `.claude/agents/*.md` and opencode's `agent/*.md`: a `---`
frontmatter with `name`, `description`, optional `model`, `access` (read or work),
`tools` (skill names, comma separated or a YAML list) and `device`, then the body, that
agent's prompt. She writes them with her notebook tools; the user sees them in the
Notebook. `sub_agents__start(template=...)` starts one (the arguments given win over the
template's), the conversation keeps the template's name (`agent_template`) and every
turn reads the note again: its body is the start of the sub-agent's system prompt
(turns/prompt.py system_blocks), its tools the skills it is offered (turns/loads.py
offered)."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..skills.notebook import read_note, unstamped

FOLDER = "templates"
ALWAYS = ("intercom", "skills")    # a template's agent keeps its brief, report and loads


@dataclass
class Template:
    name: str
    description: str = ""
    model: str | None = None
    access: str | None = None
    device: str | None = None
    tools: list[str] = field(default_factory=list)
    body: str = ""


def parse(text: str) -> Template | None:
    """A template note's text -> its Template; None without a frontmatter `name`."""
    text = unstamped(text.strip()).strip()
    if not text.startswith("---"):
        return None
    head, sep, body = text[3:].lstrip("\r\n").partition("\n---")
    if not sep:
        return None
    meta: dict[str, object] = {}
    key = None
    for line in head.splitlines():
        if line.strip().startswith("- ") and key:          # a YAML list item
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(line.strip()[2:].strip())
        elif ":" in line and not line[:1].isspace():
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            meta[key] = value if value else []
        elif line[:1].isspace() and key and isinstance(meta.get(key), str):
            meta[key] = f"{meta[key]} {line.strip()}"      # a folded line
    name = str(meta.get("name") or "").strip()
    if not name:
        return None

    def one(k: str) -> str | None:
        v = meta.get(k)
        return v.strip() or None if isinstance(v, str) else None

    tools = meta.get("tools") or []
    if isinstance(tools, str):
        tools = tools.strip("[]").split(",")
    return Template(name=name, description=one("description") or "",
                    model=one("model"), access=one("access"), device=one("device"),
                    tools=[t.strip().strip("'\"") for t in tools if t.strip()],
                    body=body.partition("\n")[2].strip())


def all_templates(cfg) -> dict[str, Template]:
    """Her templates by casefolded name, from FOLDER; the first file wins a name."""
    root = cfg.her.notebook
    base = root / FOLDER
    out: dict[str, Template] = {}
    if not base.is_dir():
        return out
    for p in sorted(base.glob("*.md")):
        if (text := read_note(root, p.relative_to(root).as_posix())) and \
                (t := parse(text)) is not None:
            out.setdefault(t.name.casefold(), t)
    return out


def find(cfg, name: str) -> Template | None:
    return all_templates(cfg).get(" ".join(name.split()).casefold())


def listing(cfg) -> str | None:
    """The templates she has, one line each, for the tail of her turns."""
    got = all_templates(cfg).values()
    if not got:
        return None
    lines = [f"- {t.name}: {t.description}" if t.description else f"- {t.name}"
             for t in got]
    return (f"Your agent templates ({FOLDER}/ in your notebook; "
            f"sub_agents__start(template=...)):\n" + "\n".join(lines))


def of(c, cid: str, cfg) -> Template | None:
    """The template a sub-agent's conversation was started from, as its note says now;
    None for any other conversation and when the note is gone."""
    row = c.execute("SELECT agent_template FROM conversations WHERE id=?",
                    (cid,)).fetchone()
    return find(cfg, row[0]) if row is not None and row[0] else None


def skills_of(t: Template | None) -> set[str] | None:
    """The skills a template's agent is offered: its `tools` and ALWAYS; None: every one."""
    return {*t.tools, *ALWAYS} if t is not None and t.tools else None
