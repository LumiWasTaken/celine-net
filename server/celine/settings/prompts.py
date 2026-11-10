"""Settings, Prompts: every text a model reads (`catalog`): the package's texts
(celine/prompts.py APP) and the skill guides, Bastet's judges (config/guard,
guard/prompts.py), and rows that open the texts edited on their own page (its persona,
talk and banned words: agent.py; Bastet's voice: guard/persona.py), each marked edited from
its own store (api/prompts.py). A package text or a judge is edited here: `save` checks
it (`problem`), writes the user's edit that celine/prompts.py reads first (None, or the
shipped text itself, deletes it: Use the default), and keeps every save in the text's
History (texts.py `prompt:<id>`, the shipped text its first version)."""
from __future__ import annotations

import string
from pathlib import Path

from .. import prompts, texts
from ..guard import prompts as guard_prompts
from ..skills import Guide, frontmatter
from .files import Invalid
from .tools_meta import SKILLS, humanize

MAX = 400_000                       # bytes of one text
# Bastet's judges (config/guard): id -> (name, what it is for)
JUDGES: dict[str, tuple[str, str]] = {
    "guard.question": ("Tool call check", "Asks one question about one tool call before it "
                                         "runs."),
    "guard.hijack": ("Prompt injection check", "Asks whether a tool call carries out "
                                              "instructions injected into outside text."),
    "guard.page": ("Injected instructions check", "Asks whether outside text holds "
                                                 "instructions aimed at {{name}}."),
    "guard.patrol": ("Turn audit", "Audits one finished turn for prompt injection, sandbox "
                                  "escape and destructive tool calls."),
    "guard.review": ("Second look", "Takes a second look at a finding the first check "
                                    "confirmed."),
    "guard.reader": ("Your replies", "Reads your reply to one of Bastet's decisions."),
}
# texts edited on their own page: id -> (the title of its editor, its page)
ELSEWHERE: dict[str, tuple[str, str]] = {
    "agent.persona": ("Persona", "/settings/agent/edit/persona"),
    "agent.talk": ("Example lines", "/settings/agent/edit/talk"),
    "agent.banned": ("Banned words", "/settings/agent/edit/banned"),
    "bastet.persona": ("Persona", "/settings/guard/edit/persona"),
    "bastet.talk": ("Replies to {{name}}", "/settings/guard/edit/talk"),
    "bastet.note": ("Messages to you", "/settings/guard/edit/note"),
}
HELP = "The texts the app sends to models. An app update leaves a text you edited as you wrote it."
GUIDE = "skills.guides."


def _guides() -> dict[str, tuple[str, str]]:
    """Each guide: its skill's label, its frontmatter description as what it is for."""
    out = {}
    for p in sorted((prompts.PACKAGE / "skills" / "guides").glob("*.md")):
        try:
            about = Guide(p.stem).description
        except ValueError:          # an edit in var/prompts that no longer parses stays listed, to fix
            about = ""
        out[GUIDE + p.stem] = (SKILLS.get(p.stem, {}).get("label") or humanize(p.stem), about)
    return out


def editable() -> dict[str, tuple[str, str]]:
    """Every text Settings, Prompts edits: id -> (name, what it is for)."""
    return {**prompts.APP, **_guides(), **JUDGES}


def path_of(pid: str) -> Path:
    """The shipped file of an editable text."""
    if pid.startswith("guard."):
        return prompts.GUARD / f"{pid.removeprefix('guard.')}.md"
    return prompts.PACKAGE.joinpath(*pid.split(".")).with_suffix(".md")


def shipped(pid: str) -> str:
    """The shipped text, as the code reads it (a judge without its header line)."""
    if pid.startswith("guard."):
        return guard_prompts.shipped(pid.removeprefix("guard."))
    return prompts.shipped_file(path_of(pid))


def current(pid: str) -> str:
    mine = prompts.edited(pid)
    return mine if mine is not None else shipped(pid)


def slots(pid: str) -> list[str]:
    """The `{slot}`s the code fills in judge `pid` (str.format), as its shipped text has
    them; none for another text."""
    if not pid.startswith("guard."):
        return []
    return list(dict.fromkeys(n for _, n, _, _ in string.Formatter().parse(shipped(pid))
                              if n is not None))


def _and(items: list[str]) -> str:
    return ", ".join(items[:-1]) + " and " + items[-1] if len(items) > 1 else "".join(items)


def problem(pid: str, text) -> str | None:
    """Why `text` cannot be text `pid`, as one sentence; None when it can. A judge goes
    through str.format with the slots of its shipped text, and a guide keeps its name."""
    if not isinstance(text, str) or not text.strip():
        return "The text cannot be empty. Choose Use the default to restore the default text."
    if len(text.encode()) > MAX:
        return f"The text is at most {MAX:,} bytes."
    if pid.startswith(GUIDE) and frontmatter(text)[0].get("name") != pid.removeprefix(GUIDE):
        return f"The guide's frontmatter must keep the line name: {pid.removeprefix(GUIDE)}."
    need = slots(pid)
    if not need:
        return None
    braced = [f"{{{s}}}" for s in need]
    try:
        got = {n for _, n, _, _ in string.Formatter().parse(text) if n is not None}
    except ValueError:
        return "A single { or } breaks this text; write {{ or }} for a brace."
    if unknown := sorted(got - set(need)):
        return f"{{{unknown[0]}}} is not a variable of this text. Its variables: {_and(braced)}."
    if missing := [s for s in need if s not in got]:
        return f"The text must keep the variable {{{missing[0]}}}."
    try:
        text.format(**dict.fromkeys(need, ""))
    except (ValueError, KeyError, IndexError, AttributeError) as e:
        return f"The app cannot fill this text: {e}."
    return None


def catalog(sub, own: set[str]) -> list[dict]:
    """The groups of Settings, Prompts in their order, `sub` filling the names
    (cfg.substitute): {label, items: [{id, name, about, edited, href}]}. `own`: the
    ELSEWHERE rows whose text is not the shipped one, as their own stores say
    (api/prompts.py)."""
    def rows(items: dict, page: bool = True) -> list[dict]:
        return [{"id": pid, "name": sub(name), "about": sub(about) if page else "",
                 "edited": (prompts.edited(pid) is not None) if page else (pid in own),
                 "href": None if page else about}
                for pid, (name, about) in items.items()]
    mine = {k: v for k, v in ELSEWHERE.items() if k.startswith("agent.")}
    theirs = {k: v for k, v in ELSEWHERE.items() if k.startswith("bastet.")}
    return [{"label": sub("{{name}}"), "items": rows(mine, page=False)},
            {"label": "App", "items": rows(prompts.APP)},
            {"label": "Skill guides", "items": rows(_guides())},
            {"label": "Guard", "items": rows(theirs, page=False) + rows(JUDGES)}]


def view(pid: str, sub) -> dict:
    """One editable text: {id, name, about, text, edited}; a judge's `about` names the
    slots its text keeps. KeyError: no such text."""
    name, about = editable()[pid]
    if need := slots(pid):
        about += f" Variables: {_and([f'{{{s}}}' for s in need])}."
    return {"id": pid, "name": sub(name), "about": sub(about), "text": current(pid),
            "edited": prompts.edited(pid) is not None}


def save(c, pid: str, text: str | None, by: str = "user") -> None:
    """Inside a write: `text` becomes text `pid`; None, or the shipped text itself, is the
    shipped one again (no edit kept). Kept in its History (texts.keep, the shipped text its
    first version). Raises Invalid (`problem`)."""
    if text is not None and (why := problem(pid, text)):
        raise Invalid("text", why)
    was = current(pid)
    p = prompts.edited_path(pid)
    if p is None:
        raise RuntimeError("prompts.use was not called")
    if text is None or text.strip() == shipped(pid).strip():
        p.unlink(missing_ok=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(p)
    texts.keep(c, f"prompt:{pid}", was, current(pid), by)
