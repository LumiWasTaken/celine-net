"""skills__read_guide and skills__load: a skill's guide with its notes on it, and
loading a skill.

Every turn it sees the skill index (the loaded skills, one line per other skill) and
every tool of the loaded skills (turns/loads.py). The guide of a skill says how its
tools go together, with examples; `skills__read_guide(name)` returns it, loaded or not.
If it has a note at `skills/<name>.md` in its notebook, it is appended to the result.
`skills__load(name)` loads a skill: its tools are in its list from its next step.
"""
from __future__ import annotations

import logging
from typing import Annotated

from . import META, SEP, ToolContext, ToolResult, guide_text, tool

log = logging.getLogger(__name__)

# Skill notes longer than this (characters) are truncated: they ride inside the same
# tool result as the guide.
_NOTE_MAX_CHARS = 4000


def skill_note_path(cfg, skill_name: str):
    """The conventional notebook path for a skill's personal notes."""
    return cfg.her.notebook / "skills" / f"{skill_name}.md"


def read_skill_note(cfg, skill_name: str) -> str | None:
    """Read skills/<name>.md from the notebook, if it exists.

    Returns the file text (capped to _NOTE_MAX_CHARS), or None when there is no note.
    Path traversal is blocked: the resolved path must stay inside the notebook.
    OS errors are swallowed and logged.
    """
    root = cfg.her.notebook.resolve()
    p = skill_note_path(cfg, skill_name).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.debug("could not read skill note %s", p, exc_info=True)
        return None
    if not text.strip():
        return None
    if len(text) > _NOTE_MAX_CHARS:
        # cut at a line boundary inside the budget
        cut = text[:_NOTE_MAX_CHARS]
        nl = cut.rfind("\n")
        if nl > _NOTE_MAX_CHARS // 2:
            cut = cut[:nl]
        return cut + f"\n\n[truncated; full note: notebook__read(path=\"skills/{skill_name}.md\")]"
    return text


class Skills:
    name = META
    description = "Shows the guide of a skill and loads a skill."

    def __init__(self, registry, cfg):
        self.registry = registry
        self.cfg = cfg

    async def _here(self, ctx: ToolContext) -> dict:
        """The skills offered in this conversation, with their tools."""
        from ..turns.loads import offered
        return {n: s for n, s in (await offered(self.registry, ctx.store,
                                                ctx.conversation_id)).items() if n != META}

    @tool()
    async def read_guide(
        self,
        ctx: ToolContext,
        name: Annotated[str, "A skill name from the skill index, such as shell."],
    ) -> ToolResult:
        """Read the guide of one skill: how its tools go together, with examples, and
        your notes on it when you keep any. The skill does not have to be loaded."""
        from ..turns import loads
        key = str(name or "").strip().lower().split(SEP)[0]
        if key == META:
            carried = await loads.select(ctx.store, self.cfg, self.registry,
                                         ctx.conversation_id, None)
            return ToolResult(content=self.registry.index(self.cfg, carried) or
                              "No skills here.", display={"skill": key})
        names = list(await self._here(ctx))
        if key not in names:
            return ToolResult(content=f"No skill called {name!r} here. The skills are: "
                                      f"{', '.join(names)}.", is_error=True)
        skill = self.registry.skills[key]
        g = getattr(skill, "guide", None)
        body = self.cfg.substitute(guide_text(self.cfg, g.body) if g is not None
                                   else str(getattr(skill, "description", "")))
        if why := self.registry.down(key):
            from .gating import down_sentence
            body = f"{self.cfg.substitute(down_sentence(key, why))}\n\n{body}"
        note = read_skill_note(self.cfg, key)
        if note:
            body += (f"\n\n---\nYour notes about {key} "
                     f"(from your notebook, skills/{key}.md):\n\n{note.strip()}")
        return ToolResult(content=f"# {key}\n\n{body}", display={"skill": key})

    # read-only to the world: it changes what it carries, nothing else, so a sub-agent
    # with read access may load too and a step with it may run again after a restart
    @tool()
    async def load(
        self,
        ctx: ToolContext,
        name: Annotated[str, "A skill name from the skill index, such as web."],
    ) -> ToolResult:
        """Load a skill: its tools are in your list from your next step and stay while
        you use them. Load one when the work needs it."""
        from ..turns import loads
        from .gating import down_why, is_enabled, overrides_for, reason
        key = str(name or "").strip().lower().split(SEP)[0]
        here = await self._here(ctx)
        if key not in here:
            return ToolResult(content=f"No skill called {name!r} here. The skills are: "
                                      f"{', '.join(here)}.", is_error=True)
        if why := self.registry.down(key):
            return ToolResult(content=self.cfg.substitute(
                f"{key} is down: {down_why(why)} Its tools stay out."), is_error=True)
        overrides = await overrides_for(ctx.store, ctx.conversation_id)
        if not any(is_enabled(s, overrides, self.cfg) for s in here[key]):
            where = ("in this conversation" if any(reason(s, overrides, self.cfg) == "here"
                                                   for s in here[key]) else "in Settings, Tools")
            return ToolResult(content=self.cfg.substitute(
                f"{key} is off {where}, so it cannot load. Ask {{{{username}}}} "
                f"to turn it on, or work without it."), is_error=True)
        await loads.touch(ctx.store, ctx.conversation_id, "skill", [key])
        return ToolResult(content=f"Loaded {key}. Its tools are in your list from your "
                                  f"next step.", display={"skill": key})
