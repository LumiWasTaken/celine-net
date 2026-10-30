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

import re
from dataclasses import dataclass, field

import yaml

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
    file: str = ""          # its note, templates/<file>.md


FENCE = re.compile(r"^---[ \t]*\r?$", re.M)     # a line of just ---


def parse(text: str) -> Template:
    """A template note's text -> its Template. Its `---` frontmatter is YAML, as in
    Claude Code's agent files. Raises ValueError with the reason it is no template."""
    text = unstamped(text.strip()).strip()
    fences = list(FENCE.finditer(text))
    if not fences or fences[0].start() != 0:
        raise ValueError("it does not start with a --- frontmatter")
    if len(fences) < 2:
        raise ValueError("its frontmatter has no closing --- line")
    head, body = text[fences[0].end() + 1:fences[1].start()], text[fences[1].end():]
    try:
        meta = yaml.safe_load(head)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" on line {mark.line + 1} of the frontmatter" if mark else ""
        raise ValueError(f"its frontmatter is not YAML{where}: "
                         f"{getattr(e, 'problem', None) or e}") from None
    if not isinstance(meta, dict):
        raise ValueError("its frontmatter is not a list of key: value lines")
    meta = {str(k).lower(): v for k, v in meta.items()}

    def one(k: str) -> str | None:
        v = meta.get(k)
        if isinstance(v, (list, dict)):
            raise ValueError(f"its {k} takes one value, not a list")
        return str(v).strip() or None if v is not None else None

    name = " ".join((one("name") or "").split())
    if not name:
        raise ValueError("its frontmatter has no name")
    tools = meta.get("tools") or []
    if isinstance(tools, str):
        tools = tools.strip("[]").split(",")
    if not isinstance(tools, list):
        raise ValueError("its tools are not a list of skill names")
    return Template(name=name, description=one("description") or "",
                    model=one("model"), access=one("access"), device=one("device"),
                    tools=[str(t).strip().strip("'\"") for t in tools if str(t).strip()],
                    body=body.strip())


def scan(cfg) -> tuple[dict[str, Template], list[str]]:
    """Her templates by casefolded name, from FOLDER, and one line for each file that
    is no template and each name two files share (neither of them starts)."""
    root = cfg.her.notebook
    base = root / FOLDER
    found: dict[str, list[tuple[str, Template]]] = {}
    problems: list[str] = []
    if not base.is_dir():
        return {}, problems
    for p in sorted(base.glob("*.md")):
        rel = p.relative_to(root).as_posix()
        if not (text := read_note(root, rel)):
            continue
        try:
            t = parse(text)
        except ValueError as e:
            problems.append(f"{rel} is no template: {e}.")
            continue
        t.file = rel
        found.setdefault(t.name.casefold(), []).append((rel, t))
    out: dict[str, Template] = {}
    for key, got in found.items():
        if len(got) == 1:
            out[key] = got[0][1]
        else:
            problems.append(f"{' and '.join(r for r, _ in got)} share the name "
                            f"{got[0][1].name!r}, so it does not start.")
    return out, problems


def all_templates(cfg) -> dict[str, Template]:
    return scan(cfg)[0]


def pick(got: dict[str, Template], name: str) -> Template | None:
    """The template called `name`, or whose note `name` is (templates/x.md, templates/x,
    x.md)."""
    key = " ".join(name.split()).casefold()
    if key in got:
        return got[key]
    path = key.removeprefix(f"{FOLDER}/").removesuffix(".md")
    return next((t for t in got.values()
                 if t.file.casefold() == f"{FOLDER}/{path}.md"), None)


def find(cfg, name: str) -> Template | None:
    return pick(all_templates(cfg), name)


def listing(cfg) -> str | None:
    """The templates she has, one line each, and the files that are none, for the tail
    of her turns."""
    got, problems = scan(cfg)
    if not got and not problems:
        return None
    lines = [f"- {t.name}: {t.description}" if t.description else f"- {t.name}"
             for t in got.values()]
    return (f"Your agent templates ({FOLDER}/ in your notebook; "
            f"sub_agents__start(template=...)):\n" + "\n".join(lines + problems))


def of(c, cid: str, cfg) -> Template | None:
    """The template a sub-agent's conversation was started from, as its note says now;
    None for any other conversation and when the note is gone."""
    row = c.execute("SELECT agent_template FROM conversations WHERE id=?",
                    (cid,)).fetchone()
    return find(cfg, row[0]) if row is not None and row[0] else None


def skills_of(t: Template | None) -> set[str] | None:
    """The skills a template's agent is offered: its `tools` and ALWAYS; None: every one."""
    return {*t.tools, *ALWAYS} if t is not None and t.tools else None
