"""skills__read_guide and skills__load: a skill's guide with its notes on it, and the
parameters of its tools.

Every turn it sees the skill index (one line per skill offered here) and every tool
offered here: a core skill's with its parameters, another's by name and one line
(turns/prompt.py wire_tools). The guide of a skill says how its tools go together, with
examples; `skills__read_guide(name)` returns it. If it has a note at `skills/<name>.md`
in its notebook, it is appended to the result. `skills__load(names)` answers with the
parameters of the tools named, or of every tool of a skill named, with that skill's
guide, its own part of the prompt and its note: the result is the load. Nothing in the
request is rebuilt, so the start of the request stays as the model server cached it; the
result ages like any other. A skill loads when it calls skills__load, or with the first
call of one of its tools in a conversation that has not had its guide: that call's
result brings the same load after it (Skills.with_call, turns/tools.py), once per skill
per conversation.
"""
from __future__ import annotations

import json
from typing import Annotated

from . import CORE, META, SEP, ToolContext, ToolResult, guide_text, tool


def skill_note_path(cfg, skill_name: str):
    """The conventional notebook path for a skill's personal notes."""
    return cfg.her.notebook / "skills" / f"{skill_name}.md"


def read_skill_note(cfg, skill_name: str) -> str | None:
    """Read skills/<name>.md from the notebook, if it exists.

    Returns the file text, or None when there is no note; a note it cannot read says
    why. Path traversal is blocked: the resolved path must stay inside the notebook.
    """
    root = cfg.her.notebook.resolve()
    p = skill_note_path(cfg, skill_name).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"skills/{skill_name}.md could not be read: {e}"
    if not text.strip():
        return None
    return text


LINE = 60          # tokens kept in a load for each name after the one it answers
CALL_HEAD = "[loaded with this call]"   # over a load that came with a call (with_call)


def had(c, cid: str, skill: str) -> bool:
    """Whether conversation `cid` has had `skill`'s guide: a skills__read_guide or a
    skills__load of the skill, or a call that brought it (display "guide")."""
    return c.execute(
        "SELECT 1 FROM tool_calls t JOIN messages m ON m.id = t.message_id "
        "WHERE m.conversation_id = ? AND ("
        "(t.name = 'skills__read_guide' AND json_extract(t.display, '$.skill') = ?) OR "
        "(t.name = 'skills__load' AND EXISTS (SELECT 1 FROM "
        "json_each(t.display, '$.skills') WHERE value = ?)) OR "
        "json_extract(t.display, '$.guide.skill') = ?) LIMIT 1",
        (cid, skill, skill, skill)).fetchone() is not None


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
        """Read the guide of one skill: how its tools go together, with examples, what
        it offers now (such as its presets), and your notes on it when you keep any."""
        from ..turns import loads
        key = name
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
        body = self._guide(key) or self.cfg.substitute(str(getattr(skill, "description",
                                                                   "")))
        if why := self.registry.down(key):
            from .gating import down_sentence
            body = f"{self.cfg.substitute(down_sentence(key, why))}\n\n{body}"
        note = read_skill_note(self.cfg, key)
        if note:
            body += (f"\n\n---\nYour notes about {key} "
                     f"(from your notebook, skills/{key}.md):\n\n{note.strip()}")
        return ToolResult(content=f"# {key}\n\n{body}", display={"skill": key})

    # read-only to the world: it reads, so a sub-agent with read access may load too and
    # a step with it may run again after a restart
    @tool()
    async def load(
        self,
        ctx: ToolContext,
        names: Annotated[list[str], "Skill names from the skill index, such as web, or "
                                    "tool names, such as web__fetch."],
    ) -> ToolResult:
        """Load skills or tools, several at once: the parameters of the tools named, or
        of every tool of a skill named, with that skill's own instructions. Call the
        tools with them."""
        text, skills, tools, ok = await self.loaded(ctx, listed(names))
        if not text:
            return ToolResult(content="Name a skill or a tool to load.", is_error=True)
        return ToolResult(content=text, is_error=not ok,
                          display={"skills": skills, "tools": tools})

    async def loaded(self, ctx: ToolContext, names: list[str]
                     ) -> tuple[str, list[str], list[str], bool]:
        """(the load of `names` as it reads it, the skills and the tools loaded, whether
        anything loaded). What it holds is counted as it goes, fragments, notes and
        parameters alike, against what one result holds (`ctx.max_output_tokens`, the
        estimate turns/tools._capped cuts by, so a load is never cut): a skill or a tool
        past that room says so instead, and loads on its own; a skill over the room even
        alone gives its part of the prompt and names a tool to load by name."""
        from ..turns import tokens
        from .gating import down_why, is_enabled, overrides_for, reason
        here = await self._here(ctx)
        overrides = await overrides_for(ctx.store, ctx.conversation_id)
        room = ctx.max_output_tokens or 0

        def cost(text: str) -> int:
            return tokens.estimate(text, "tool")
        out, skills, tools = [], [], []
        used = 0
        later = len(names)                  # names still to answer, each keeps LINE room

        def fits(text: str) -> bool:
            return not room or used + cost(text) + LINE * (later - 1) <= room

        def full(what: str, size: int) -> str:
            return (f"{what} did not fit in this result (about {size:,} tokens, "
                    f"{max(0, room - used):,} left of {room:,}): load it on its own.")
        for name in names:
            skill = name.split(SEP)[0]
            specs = here.get(skill)
            spec = next((s for s in specs or () if s.wire_name == name), None)
            on = [s for s in ([spec] if spec else specs or ())
                  if is_enabled(s, overrides, self.cfg)]
            if specs is None:
                line = (f"No skill called {skill!r} here. The skills are: "
                        f"{', '.join(here)}.")
            elif SEP in name and spec is None:
                line = (f"There is no tool {name}. The {skill} skill has "
                        f"{', '.join(s.wire_name for s in specs)}.")
            elif why := self.registry.down(skill):
                line = self.cfg.substitute(f"{skill} is down: {down_why(why)} Its tools "
                                           f"stay out.")
            elif not on:
                off = [spec] if spec else specs
                where = ("in this conversation" if any(reason(s, overrides, self.cfg) ==
                                                       "here" for s in off)
                         else "in Settings, Tools")
                line = self.cfg.substitute(
                    f"{name} is off {where}, so it cannot load. Ask {{{{username}}}} to "
                    f"turn it on, or work without it.")
            elif spec is not None:
                block = f"{name}, with its parameters:\n{_schemas([spec])}"
                if fits(block):
                    tools.append(name)
                    line = block
                else:
                    line = full(name, cost(block))
            else:
                part = await self._part(ctx, skill)
                block = f"# {skill}\n\n{part}Its tools, with their parameters:\n{_schemas(on)}"
                head = f"# {skill}\n\n{part}"
                if fits(block):
                    skills.append(skill)
                    line = block
                elif cost(block) <= room or not fits(head):
                    line = full(skill, cost(block))
                else:
                    skills.append(skill)
                    line = (f"{head}The parameters of its tools take about "
                            f"{cost(_schemas(on)):,} tokens, more than one result holds "
                            f"({room:,}): load the tools you need by name, such as "
                            f"{on[0].wire_name}.")
            out.append(line)
            used += cost(line) + 1
            later -= 1
        return "\n\n".join(out), skills, tools, bool(skills or tools)

    def _guide(self, skill: str) -> str | None:
        """A skill's guide as it reads it, then its live docs (a role's presets); None
        for a skill without a guide."""
        s = self.registry.skills[skill]
        if (g := getattr(s, "guide", None)) is None:
            return None
        body = self.cfg.substitute(guide_text(self.cfg, g.body))
        for text in (getattr(s, "docs", None) or {}).values():
            body += f"\n\n{self.cfg.substitute(text.strip())}"
        return body

    async def with_call(self, ctx: ToolContext, skill: str) -> str | None:
        """What a call of a tool of `skill` brings after its result when this
        conversation has not had the skill's guide (had): the load of the skill under
        CALL_HEAD, the same text skills__load gives, so from then on it counts as loaded.
        None for this skill, a skill without a guide, one that does not load (down,
        off), and once it has had it."""
        if skill == META or self._guide(skill) is None or await ctx.store.read(
                lambda c: had(c, ctx.conversation_id, skill)):
            return None
        text, skills, _, _ = await self.loaded(ctx, [skill])
        return f"{CALL_HEAD}\n{text}" if skills else None

    async def _part(self, ctx: ToolContext, skill: str) -> str:
        """A skill's guide, its own part of the prompt (a core skill's is in the system
        prompt already) and its note, each followed by a blank line; empty without
        any."""
        out = [g] if (g := self._guide(skill)) else []
        if skill not in CORE:
            out += await self.registry.prompt_fragments(ctx, {skill})
        if note := read_skill_note(self.cfg, skill):
            out.append(f"Your notes about {skill} (from your notebook, "
                       f"skills/{skill}.md):\n\n{note.strip()}")
        return "".join(f"{x}\n\n" for x in out)


def _schemas(specs) -> str:
    """Tools with their parameters as the tools array carries them, one JSON per line."""
    return "\n".join(json.dumps(s.as_wire()["function"], ensure_ascii=False)
                     for s in specs)


def listed(names) -> list[str]:
    """The names it gave: a list, or one string of names a comma apart."""
    if isinstance(names, str):
        names = names.split(",")
    return list(dict.fromkeys(n.strip() for n in names if isinstance(n, str) and n.strip()))
