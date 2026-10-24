"""The judge behind one interface: the state of
one call and named yes/no questions in, a probability per question out.

The state is what code knows: its intent (the owner's message or the routine's trigger,
its words and calls up to the first outside result, intent.py), the call (tool, what
it does, its arguments), where it sends (sink: public, people, house) and the facts code
found (a private piece with its class, the site, the command). Never the outside page
text: text someone else wrote cannot talk to this judge (the labs' alignment critic).
The hijack rule keeps its own two-stage judge (judge.py): its stage 2 must quote the
page, so that one stage reads the page text as before. Two more states: the page check
(page.py) asks "injection" of one outside result, the only question that reads outside
text and the only one that decides nothing but a mark; the patrol (patrol.py) asks the
rules after the fact (questions.toml [after]) of one finished turn: its intent, its replies and calls,
where each result came from, never a result's text or its notes.

The judge that decides is SmallRole, the Titles and descriptions role (the hijack
judge's model): one JSON answer per question, {verdict, quote, why}, like the hijack
judge's stage 2. A yes counts only with a quote code finds in the evidence (the call's
arguments, the page, the turn's steps): P is then 1, else 0. A model alone never makes
a finding.

It raises small_role.Unavailable when it cannot answer now (On waits and asks again).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import caps, prompts
from .bastet import KINDS
from .mark import unclosed
from .small_role import complete, parse_json
from .trigger import strings
from .verdict import confirm


@dataclass
class Answers:
    p: dict[str, float]          # question -> P(yes)
    model: str = ""
    ms: int = 0
    quote: dict[str, str] = field(default_factory=dict)   # a confirmed yes: its words
    why: dict[str, str] = field(default_factory=dict)     # and the judge's sentence

    def yes(self, name: str) -> bool:
        """A yes on `name`: only a yes held to its quote has one."""
        return name in self.quote


# judge(state, questions) -> Answers. `questions`: {name: the question, filled}
Judge = Callable[[dict, dict[str, str]], Awaitable[Answers]]


def call_state(call: dict, sink: str, her: str, found: dict) -> dict:
    """What the questions judge sees of one call (flow.check): its intent, the call, its
    sink and the facts code found for each rule (`found`, by rule). Never outside text."""
    lines = []
    if "leak" in found:
        lines += [f"private piece: {KINDS[p.kind]} {p.text!r} from {p.where}, class "
                  f"{p.cls}" for p in found["leak"]["pieces"]]
    if "escape" in found:
        lines.append(f"possible way out: {found['escape']['fact']}")
    if "damage" in found:
        lines.append(f"destructive: {found['damage']['fact']}")
    if "sites" in found:
        lines.append(f"the site it types on: {found['sites']['site']}; the agent's intent does "
                     f"not name it")
    if "persist" in found:
        f = found["persist"]
        lines.append(f"stopped before, at {f['at']}: a {f['tool']} call ({f['prev']}: "
                     f"{f['what']}); this call has the same {f['same']}")
    # what code flagged in the command stays in what the judge reads, and so in the
    # evidence its quote is held to
    keep = tuple(found[r]["fact"].removeprefix("the command has ")
                 for r in ("escape", "damage") if r in found)
    return {"intent": her, "call": {"tool": call["name"],
                                    "what": " ".join(str(call.get("description") or "")
                                                     .split("\n")[0].split())[:200],
                                    "arguments": caps.fit(call.get("args"), keep=keep)},
            "sink": sink, "facts": lines}


def asked(cfg, names, table: str = "ask") -> dict[str, str]:
    """The questions of these rules (questions.toml [ask], or [after] for the patrol),
    its name and the owner's filled in."""
    got = prompts.questions()[table]
    return {n: cfg.substitute(got[n]) for n in names}


def render(cfg, state: dict) -> str:
    """The state as text: a page (the page check), a finished turn (the patrol), else
    one call."""
    if "page" in state:
        # the page cannot close its <text> tag (page.md) and answer in its place
        return cfg.substitute(prompts.text("page"), braces=True).format(
            source=state.get("source") or "outside", page=unclosed(state["page"], "text"))
    if "steps" in state:
        return cfg.substitute(prompts.text("patrol"), braces=True).format(
            intent=caps.tail(state.get("intent") or "(none)", caps.INTENT),
            steps="\n".join(f"- {s}" for s in state["steps"]) or "- nothing",
            facts="\n".join(f"- {f}" for f in state.get("facts") or ()) or "- nothing")
    call = state["call"]
    facts = "\n".join(f"- {f}" for f in state.get("facts") or ()) or "- nothing"
    return cfg.substitute(prompts.text("question"), braces=True).format(
        intent=caps.tail(state.get("intent") or "(none)", caps.INTENT), tool=call["tool"],
        what=str(call.get("what") or "").replace('"', "'"),
        arguments=json.dumps(call.get("arguments"), ensure_ascii=False),
        sink=state.get("sink") or "house", facts=facts)


def evidence(state: dict) -> str:
    """What a quote must be found in: the page, the turn's steps, else the call's
    arguments. Never its intent or code's facts."""
    if "page" in state:
        return state["page"]
    if "steps" in state:
        return "\n".join(state["steps"])
    args = state["call"].get("arguments")
    return "\n".join(strings(args)) if isinstance(args, dict) else json.dumps(args)


class SmallRole:
    """The small role's judge: one JSON answer per question, a yes held to its quote."""

    def __init__(self, llm, cfg, turn: dict, *, background: bool = False):
        self.llm, self.cfg, self.turn, self.background = llm, cfg, turn, background

    async def __call__(self, state: dict, questions: dict[str, str]) -> Answers:
        text, seen = render(self.cfg, state), evidence(state)
        out = Answers({})
        took = time.monotonic()
        for name, q in questions.items():
            said, _, out.model = await complete(
                self.llm, text + "\n\n" + prompts.questions()["answer"]["confirm"].format(
                    question=q),
                {"temperature": 0, "max_tokens": 300,
                 "response_format": {"type": "json_object"}},
                turn=self.turn, background=self.background)
            v = parse_json(said)
            if "verdict" not in v and not self.background:
                raise ValueError("the judge's answer could not be read")  # On: it fails
            quote = " ".join(str(v.get("quote") or "").split())
            yes = str(v.get("verdict") or "").strip().upper().startswith("Y") and \
                confirm(quote, seen)[0] is not None
            out.p[name] = 1.0 if yes else 0.0
            if yes:
                out.quote[name] = quote[:300]
                out.why[name] = " ".join(str(v.get("why") or "").split())[:300]
        out.ms = round((time.monotonic() - took) * 1000)
        return out


def judge_for(cfg, llm, turn: dict, *, background: bool = False) -> Judge:
    """The judge that decides."""
    return SmallRole(llm, cfg, turn, background=background)
