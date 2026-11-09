"""A text a model reads is code: a .md file in the package beside the module that reads
it (turns/title.md, kb/describe.md, skills/writer.md, ...), read once. `text(__file__,
"name.md")` is that file; `fill` puts the code's values into its `{{placeholders}}`,
then the names and the agent's pronouns (config.substitute). A placeholder nothing fills
is a Problem (cfg.problems, the Settings notice, naming the text's entry on Settings,
Prompts: `APP`) and the text goes out as it is; a save of the text drops it (`settled`).

The user edits a package text or one of Bastet's judges on Settings, Prompts
(settings/prompts.py): the edit is `<data>/prompts/<id>.md`, read here before the shipped
file (by the file's mtime, so it applies at its next use; guard/prompts.py reads the
judges' the same way); deleting it (Use the default) reads the shipped one again. An app
update changes only the shipped files, so an edited text stays as the user wrote it. An
id is the file's path in dots: turns.title, skills.guides.web, guard.hijack.

The texts a user edits elsewhere are not stored here: a Routine's prompts are its
schedule, the notebook seed config/notebook_seed/ (skills/notebook.py).
"""
from __future__ import annotations

import re
from functools import cache
from pathlib import Path

from .config import CONFIG_DIR, Problem

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
# config.substitute's own, filled when a request is built
SUBSTITUTED = frozenset(("username", "user", "name", "char", "agent_name", "pronouns"))

PACKAGE = Path(__file__).parent
GUARD = CONFIG_DIR / "guard"
# the package's texts that are not skill guides: id -> (name on Settings, Prompts, what it
# is for); tests/test_prompts_page.py holds it equal to the files
APP: dict[str, tuple[str, str]] = {
    "turns.title": ("Chat titles", "Names a chat in a few words."),
    "turns.compact": ("Chat summary", "Writes the memory of a long chat when its older "
                                      "messages no longer fit."),
    "turns.ask_before_setup": ("Ask before setting up", "Added while Ask before setting up "
                                                        "services is on."),
    "turns.pronouns": ("Pronouns", "Gives {{name}} the pronouns set in Settings."),
    "kb.describe": ("Document cards", "Describes a document in a few sentences, so Search "
                                      "finds it."),
    "kb.describe_part": ("Document parts", "Describes one part of a long document."),
    "kb.describe_part_late": ("Later document parts", "Names the topics of a part from the "
                                                      "second half of a long document."),
    "kb.describe_check": ("Spoiler check", "Takes the spoilers out of a document's card."),
    "kb.image": ("Photo descriptions", "Describes a photo, so Search finds it by what it "
                                       "shows."),
    "vision": ("Questions about an image", "Answers one question about one image."),
    "missions.steps": ("How a mission goes", "The steps every mission follows."),
    "schedules.final": ("A run's final answer", "Tells a routine run that its last answer "
                                                "is its result."),
    "schedules.note": ("A run's note", "Added when a routine writes its result into a "
                                       "note."),
    "schedules.background": ("A run in the background", "Tells a routine run that "
                             "{{username}} is not there and how to reach them."),
    "schedules.reviewer_first": ("Talk with the reviewer", "Tells a routine run that it "
                                "talks with its reviewer first."),
    "schedules.talk_over": ("End of the talk", "Asks a routine run for its final answer "
                            "after the reviewer's talk."),
    "schedules.asked": ("What the reviewer reads", "Heads the run's prompt in the "
                        "reviewer's instructions."),
    "schedules.line": ("The reviewer's line", "Tells the reviewer which line of the talk "
                       "it writes."),
    "schedules.last_line": ("The reviewer's last line", "Tells the reviewer that its next "
                            "line is its last."),
    "skills.mind_appraisal": ("Mood", "Reads one exchange and names how it made {{name}} "
                                      "feel."),
    "skills.mind_places": ("Places in the app", "The places of the app, as you see them."),
    "skills.writer": ("Writing", "Drafts a post or a message in {{name}}'s own voice."),
    "sub_agents.fragment": ("Sub-agent instructions", "Frames the instructions a sub-agent "
                                                      "gets."),
    "sub_agents.drone": ("Device sub-agent instructions", "Frames the instructions a "
                                                          "sub-agent on a device gets."),
}
_edited: Path | None = None                       # <data>/prompts, set by settings.apply
_cache: dict[Path, tuple[int, str]] = {}


def use(data_dir) -> None:
    """Where the user's edits live: <data>/prompts (settings.apply, at every load)."""
    global _edited
    _edited = Path(data_dir) / "prompts"


def edited_path(pid: str) -> Path | None:
    return _edited / f"{pid}.md" if _edited is not None else None


def edited(pid: str) -> str | None:
    """The user's edit of text `pid`, None when there is none."""
    p = edited_path(pid)
    try:
        t = p.stat().st_mtime_ns if p is not None else None
    except FileNotFoundError:
        return None
    if t is None:
        return None
    got = _cache.get(p)
    if got is None or got[0] != t:
        got = (t, p.read_text())
        _cache[p] = got
    return got[1]


def id_of(path: Path) -> str | None:
    """A text file's id (its path in dots), None for a file outside the package and
    config/guard (a plugin's guide)."""
    for root, head in ((PACKAGE, ""), (GUARD, "guard.")):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        return head + ".".join(rel.with_suffix("").parts)
    return None


@cache
def shipped_file(path: Path) -> str:
    """A shipped text file, read once."""
    return path.read_text()


@cache
def _where(module_file: str, name: str) -> tuple[Path, str | None]:
    path = Path(module_file).parent / name
    return path, id_of(path)


def text(module_file: str, name: str) -> str:
    """The package file `name` beside the module at `module_file` (its __file__), or the
    user's edit of it."""
    path, pid = _where(module_file, name)
    mine = edited(pid) if pid is not None else None
    return mine if mine is not None else shipped_file(path)


def fill(cfg, module_file: str, name: str, **values) -> str:
    """`name` beside `module_file` with every `{{key}}` of `values` in, then the names
    and pronouns (cfg.substitute). A `{{placeholder}}` left over is a Problem, once."""
    out = text(module_file, name)
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", str(value))
    if cfg is not None:
        out = cfg.substitute(out)
        left = sorted({m for m in _PLACEHOLDER.findall(out) if m not in SUBSTITUTED})
        if left:
            pid = _where(module_file, name)[1]
            where = _entry(cfg, pid) if pid in APP else f"{Path(module_file).parent.name}/{name}"
            p = Problem(where, f"nothing fills {{{{{left[0]}}}}}")
            if p not in cfg.problems:
                cfg.problems.append(p)
    return out


def _entry(cfg, pid: str) -> str:
    """How the Settings notice names package text `pid`: its entry on Settings, Prompts."""
    return f"Prompts, {cfg.substitute(APP[pid][0])}"


def settled(cfg, pid: str) -> None:
    """Text `pid` was saved: its Problem goes, and its next use says it again if it is
    still there."""
    if pid in APP:
        cfg.problems[:] = [p for p in cfg.problems if p.file != _entry(cfg, pid)]
