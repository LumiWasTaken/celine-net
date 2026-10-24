"""Bastet, the guard. This file is the port of the call flow (on_call, check, closed,
the switch, the verdict). The rest of the app reaches the other concerns by their own
module, one concern each, never through flow.py: the freeze (freeze.py), the labels on
outside text (mark.py, sources.py), the owner's proof (owner.py), the banners
(banners.py, view.py), its rows (store.py), its intercom answers (talk.py), the patrol,
the rule file (rules.py) and HTTP (api.py). Turning Bastet off is the General switch
(switch.py): off, none of it runs.

One call, from the turn's tool chokepoint (turns/tools.py), through calls.py and nothing
else: `calls.frozen` before the house's own checks, `calls.at_call` right before the call
runs (-> calls.Gate: a refusal, a pending wait, or the lines its result gains). calls.py
asks the flow:
    v = await guard.closed(turn, call, request, mode=..., store=..., cfg=...)
    v = v or await guard.on_call(turn, call, request, mode=..., app=..., store=...,
                                 cfg=..., registry=..., progress=...)
`call` is {id, name, args, description, read_only, message_id, sends}. None: no rule had
a reason to look; else the Verdict (verdict.py) its rows are written for, and calls.gated
acts on it: v.decision allow / would_stop / failed runs the call, stop refuses it with
v.sentence, asked waits for the owner (v.ask), v.freeze freezes it (freeze.freeze),
v.note goes to the owner (talk.to_owner) unless the second look takes it
(rules.REVIEWED, chain.after_stop).

The modules, one concern each:
- switch.py   the one switch: General `guard` off, watch or on
- rules.py    guard.toml, and TABLE: every rule's words and which parts take it
- calls.py    one call of its turn: frozen, at_call (gates, the judges in Watch or On,
  the owner's confirm, the freeze, the passes it has not heard of)
- flow.py     one call: closed gates, facts, passes, judges, verdict, the stops rule
- verdict.py  Judgement, Verdict, Ruling, the evidence rule (confirm)
- facts.py, persist.py, trigger.py, sources.py, patterns.py   plain code, no model
- caps.py     what a judge reads, capped, a marker at every cut
- prompts.py  the prompt files (config/guard/*.md, questions.toml)
- small_role.py  the small role's transport, its JSON, Unavailable
- intent.py   its intent and a finished turn, as the judges read them
- judge.py    the hijack judge (two stages, a quote as evidence)
- questions.py  the questions judge (the small role)
- chain.py    the second look by a big model
- page.py, patrol.py, mark.py   the page check, the patrol, the labels
- store.py    every query on guard_decisions, kv and intercom; the banners' rows
- freeze.py, passes.py, owner.py   the freeze, passes, the owner's proof
- learn.py    past decisions: lift a freeze itself or ask {{user}}
- consult.py  their reply in its intercom thread is their answer to it
- persona.py  its persona, replies and notes as the owner's texts (Settings, Guard)
- bastet.py, view.py, talk.py, banners.py, awareness.py   its voice: sentences, the
  owner's lines and banners, its intercom replies, the owner's answers, the lines in
  Celine's context
- api.py      HTTP
"""
from __future__ import annotations

from .flow import check, closed, on_call
from .switch import MODES, enabled, mode
from .verdict import Judgement, Verdict, confirm

__all__ = ["MODES", "Judgement", "Verdict", "check", "closed", "confirm", "enabled",
           "mode", "on_call"]
