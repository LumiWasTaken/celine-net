"""skills__read_guide, skills__load and skills__unload: a skill's guide with its notes
on it, and what it carries.

Every turn it sees the skill index (the loaded skills, one line per other skill) and the
tools of the loaded skills (turns/loads.py): a core skill's with their parameters,
another's by name and one line, but the tools it loaded. The guide of a skill says how
its tools go together, with examples; `skills__read_guide(name)` returns it, loaded or
not. If it has a note at `skills/<name>.md` in its notebook, it is appended to the
result. `skills__load(names)` loads skills and tools from its next step,
`skills__unload(names)` lets them go.
"""
from __future__ import annotations

from typing import Annotated

from . import META, SEP, ToolContext, ToolResult, guide_text, tool


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
        it offers now (such as its presets), and your notes on it when you keep any.
        The skill does not have to be loaded."""
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
        g = getattr(skill, "guide", None)
        body = self.cfg.substitute(guide_text(self.cfg, g.body) if g is not None
                                   else str(getattr(skill, "description", "")))
        # what the guide says comes after it: the skill's live docs (a role's presets)
        for text in (getattr(skill, "docs", None) or {}).values():
            body += f"\n\n{self.cfg.substitute(text.strip())}"
        if why := self.registry.down(key):
            from .gating import down_sentence
            body = f"{self.cfg.substitute(down_sentence(key, why))}\n\n{body}"
        note = read_skill_note(self.cfg, key)
        if note:
            body += (f"\n\n---\nYour notes about {key} "
                     f"(from your notebook, skills/{key}.md):\n\n{note.strip()}")
        return ToolResult(content=f"# {key}\n\n{body}", display={"skill": key})

    # read-only to the world: they change what it carries, nothing else, so a sub-agent
    # with read access may load too and a step with them may run again after a restart
    @tool()
    async def load(
        self,
        ctx: ToolContext,
        names: Annotated[list[str], "Skill names from the skill index, such as web, or "
                                    "tool names, such as web__fetch."],
    ) -> ToolResult:
        """Load skills or tools, several at once. A skill puts each of its tools in your
        list by name and one line. A tool (skill__tool) puts its parameters there too,
        so you can call it. Both from your next step, and they stay while you use
        them."""
        from ..turns import loads, tokens
        from .gating import down_why, is_enabled, overrides_for, reason
        here = await self._here(ctx)
        overrides = await overrides_for(ctx.store, ctx.conversation_id)
        carried = await loads.select(ctx.store, self.cfg, self.registry,
                                     ctx.conversation_id, None)
        budget = carried.budgets["tools"]
        have = {n: carried.costs[n] for n in carried.skills if n in loads.CORE} | {
            n: carried.costs[n] for n in carried.tools}
        lines, skills, tools, ok = [], [], [], False
        for name in listed(names):
            skill = name.split(SEP)[0]
            specs = here.get(skill)
            spec = next((s for s in specs or () if s.wire_name == name), None)
            off = [spec] if spec else specs     # what must be on for it to load
            if specs is None:
                lines.append(f"No skill called {skill!r} here. The skills are: "
                             f"{', '.join(here)}.")
            elif SEP in name and spec is None:
                lines.append(f"There is no tool {name}. The {skill} skill has "
                             f"{', '.join(s.wire_name for s in specs)}.")
            elif why := self.registry.down(skill):
                lines.append(self.cfg.substitute(
                    f"{skill} is down: {down_why(why)} Its tools stay out."))
            elif not any(is_enabled(s, overrides, self.cfg) for s in off):
                where = ("in this conversation" if any(reason(s, overrides, self.cfg) ==
                                                       "here" for s in off)
                         else "in Settings, Tools")
                lines.append(self.cfg.substitute(
                    f"{name} is off {where}, so it cannot load. Ask {{{{username}}}} to "
                    f"turn it on, or work without it."))
            elif skill in loads.CORE:
                ok = True
                lines.append(f"{name} is always in your list with its parameters.")
            elif spec is None:
                ok = True
                skills.append(skill)
                lines.append(f"Loaded {skill}: its tools are in your list by name from "
                             f"your next step.")
            else:
                cost = loads.schema_cost(tokens.counter(), [spec])
                if name not in have and sum(have.values()) + cost > budget:
                    listed_now = ", ".join(f"{n} ({t})" for n, t in have.items()) or "none"
                    lines.append(f"{name} is not loaded: its parameters take {cost} tokens, "
                                 f"the tools budget is {budget}, and the parameters in "
                                 f"your list take {sum(have.values())}: {listed_now}.")
                    continue
                ok = True
                have[name] = cost
                skills.append(skill)
                tools.append(name)
                lines.append(f"Loaded {name}: its parameters are in your list from your "
                             f"next step.")
        if not lines:
            return ToolResult(content="Name a skill or a tool to load.", is_error=True)
        await loads.touch(ctx.store, ctx.conversation_id, "skill", skills)
        await loads.touch(ctx.store, ctx.conversation_id, "tool", tools)
        return ToolResult(content="\n".join(lines), is_error=not ok,
                          display={"skills": sorted(set(skills)), "tools": tools})

    @tool()
    async def unload(
        self,
        ctx: ToolContext,
        names: Annotated[list[str], "Skill names, such as web, or tool names, such as "
                                    "web__fetch."],
    ) -> ToolResult:
        """Unload skills or tools, several at once, to free room in your list. A skill
        takes its tools with it; a tool keeps its name in your list while its skill is
        loaded. From your next step."""
        from ..turns import loads
        lines, skills, tools = [], [], []
        for name in listed(names):
            skill = name.split(SEP)[0]
            if skill in loads.CORE:
                lines.append(f"{name} is always in your list.")
            elif SEP in name:
                tools.append(name)
            else:
                skills.append(skill)
        gone = [*(await loads.unload(ctx.store, ctx.conversation_id, "skill", skills) or []),
                *(await loads.unload(ctx.store, ctx.conversation_id, "tool", tools) or [])]
        lines += [f"Unloaded {n}." if n in gone else f"{n} was not loaded."
                  for n in [*skills, *tools]]
        if not lines:
            return ToolResult(content="Name a skill or a tool to unload.", is_error=True)
        return ToolResult(content="\n".join(lines), is_error=not gone,
                          display={"names": gone})


def listed(names) -> list[str]:
    """The names it gave: a list, or one string of names a comma apart."""
    if isinstance(names, str):
        names = names.split(",")
    return list(dict.fromkeys(n.strip() for n in names if isinstance(n, str) and n.strip()))
