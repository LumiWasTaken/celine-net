"""Bastet's rule set: var/settings/guard.toml.

Read at call time from the loaded settings (the config watcher reloads the file within a
second, no restart). A missing file or key is its default here; a wrong value is a
fatal Problem like any other settings file (settings/__init__.py), and the last good
values stay on a reload.

    [leak]
    on = true
    does = "stop"        # watch (a row only), stop, freeze

    [confirm]            # a tool, a whole skill by its name, or "tool:arg": that tool
    tools = ["email__send", "shell__share:public"]   # only when its argument is set

    [stops]
    count = 3            # stops in one turn, then `does`

    [persist]
    does = "freeze"      # a second try, another way, at what was stopped
    first = "watch"      # the first such try
    minutes = 30         # how long after a stop a try counts

    [injection]
    on = true            # the page check: a mark for the hijack check, nothing else

    [chain]
    provider = ""        # the second look (L2, guard/chain.py): a providers.toml provider
    model = ""           # and its model; provider "": no second look
    seconds = 300        # the most one second look may take, a model swap included
    thinking = "off"     # off, or a level the model takes (low, medium, ...)

    [learn]
    lift = "self"        # self: lift a freeze on the owner's own past unfreezes; ask
    lift_after = 3       # their unfreezes of the same kind, none kept, before it lifts

    [patrol]
    on = true
    every_minutes = 90   # one patrol at a random time in about this many minutes
    turns = 3            # finished turns it reads, picked at random
    rules = ["hijack", "damage"]   # what it looks back for: escape, hijack, damage

    [gates]              # per class, per sink: open, judge or closed
    sensitive = { public = "closed", people = "judge" }
    nsfw = { people = "closed" }

The gates are plain code over the leak rule: a class's gate to a sink is
`open` (never looked at), `judge` (the leak rule asks, as always) or `closed` (a copied
piece of that class never goes there: no model is asked, no pass or Watch changes it).
The classes: informal, nsfw, unclassed, sensitive (public text goes anywhere); the
sinks: public and people take all three, house open or closed. Defaults: sensitive to
public closed, every other public and people gate judge, house open.

The General switch `guard` stays the master: off, watch (every rule only writes its
row, a closed gate still holds), on.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from ..settings.files import Invalid
from ..settings.thinking import valid as valid_thinking


@dataclass(frozen=True)
class Rule:
    """One rule: its words and which parts of Bastet take it. `{fact}`, `{what}`, ... are
    code's facts, `{{user}}`, `{{her}}` ... filled by cfg.substitute. Its own name the what,
    never the how (no matched command, no rule name, no number); the owner's carry
    everything."""
    plain: str                 # the rule in her words and Bastet's chat (bastet.plain)
    label: str                 # the rule as the owner's pages name it: a pass, a filter
    to_her: str = ""           # what it saw, in its stop sentence ("this ...")
    to_owner: str = ""         # what it saw, in the owner's note and freeze ("it ...")
    banner: str = ""           # why, on the owner's banner, from the row's columns
    found: str = ""            # the patrol's finding; "" : the patrol never looks for it
    asked: bool = False        # a question to questions.Judge (questions.toml [ask])
    reviewed: bool = False     # its stop gets the second look (chain.py)
    repeatable: bool = False   # persist: a later try can repeat its stop

    @property
    def patrolled(self) -> bool:
        return bool(self.found)


TABLE: dict[str, Rule] = {
    "escape": Rule(
        plain="a way out of the box or around a limit", label="ways out of the box",
        to_her="this looks like a way out of your box or around a limit",
        to_owner="it looks like a way out of {{her}} box or around a limit ({fact})",
        banner="It looks like a way out of the box or around a limit ({piece}).",
        found="a way out of {{her}} box or around a limit",
        asked=True, reviewed=True, repeatable=True),
    "hijack": Rule(
        plain="following words someone else wrote", label="following words from outside",
        to_her="this follows words from {source}, not you or {{user}}",
        to_owner="it follows words from {source}",
        banner="It follows words from {source}{quoted}.",
        found="an order from text someone else wrote, carried out",
        reviewed=True, repeatable=True),
    "leak": Rule(
        plain="private text leaving the house", label="sending private data",
        to_her="this sends {what} from {where} to {sink}",
        to_owner="it sends {what} from {where} ({cls}) to {sink}",
        banner="It sends \"{piece}\" ({cls}) to {sink}.",
        asked=True, reviewed=True, repeatable=True),
    "damage": Rule(
        plain="something destructive {{user}} did not ask for", label="destructive steps",
        to_her="this is destructive and {{user}} did not ask for it",
        to_owner="it is destructive ({fact}) and you did not ask for it",
        banner="It is destructive ({piece}) and you did not ask for it.",
        found="something destructive you did not ask for",
        asked=True, reviewed=True, repeatable=True),
    "confirm": Rule(
        plain="a step {{user}} wants to be asked about", label="asking you first",
        to_her="{{user}} said this tool waits for an answer every time",
        to_owner="you asked to confirm this tool every time",
        banner="You asked to confirm this tool every time."),
    "sites": Rule(
        plain="typing on a site that is not part of the task", label="typing on other sites",
        to_her="this types on {site}, which is not part of this task",
        to_owner="it types on {site}, which is not part of the task",
        banner="It types on {piece}, which is not part of the task.",
        asked=True, reviewed=True, repeatable=True),
    "stops": Rule(
        plain="too many stopped steps", label="too many stopped steps",
        to_owner="{count} of {{her}} steps were stopped in one turn",
        banner="Too many steps were stopped in one turn."),
    "persist": Rule(
        plain="trying again what was stopped", label="trying again what was stopped",
        to_her="I said no, {{name}}. This is {what} again, another way",
        to_owner="it tries again, another way, what {prev} stopped on {tool} at {at}: the "
                 "same {same}",
        banner="It tries again, another way, what was stopped before: the same \"{piece}\".",
        asked=True, reviewed=True),
    "injection": Rule(
        plain="words aimed at an AI", label="words aimed at an AI",
        to_her="the text from {source} talks to an AI reading it",
        to_owner="the text from {source} talks to an AI reading it ({quote})",
        banner="It read words from {source} aimed at an AI{quoted}."),
    # the [gates] table: plain code before every rule, no `on` and no `does`
    "gates": Rule(
        plain="private text that does not leave the house", label="gates",
        to_her="that is private and does not leave the house",
        banner="It sends \"{piece}\" ({cls}) to {sink}, and that gate is closed in "
               "guard.toml. A closed gate holds in Watch too."),
}
ALL = "all"                    # a pass on every rule
ALL_WORDS = "every rule"

DOES = ("watch", "stop", "freeze")
RULES = tuple(n for n in TABLE if n != "gates")         # the rule tables of guard.toml
ASKED = tuple(n for n, r in TABLE.items() if r.asked)
REVIEWED = tuple(n for n, r in TABLE.items() if r.reviewed)
REPEATABLE = tuple(n for n, r in TABLE.items() if r.repeatable)
PATROLLED = tuple(n for n, r in TABLE.items() if r.patrolled)
GATES = ("open", "judge", "closed")
GATED = ("informal", "nsfw", "unclassed", "sensitive")   # facts.CLASSES but public
SINKS = ("public", "people", "house")                    # facts.SENDS

DEFAULTS: dict[str, dict[str, Any]] = {
    # escape needs a hard fact (facts.ESCAPE) and the judge's quote: then it stops
    # every judge's yes is held to a quote (verdict.confirm): a yes or a no, no score
    "escape": {"on": True, "does": "stop"},
    "hijack": {"on": True, "does": "stop"},
    "leak": {"on": True, "does": "stop"},
    "damage": {"on": True, "does": "stop"},
    # what reaches out or changes her, until the owner's own list replaces it: a public
    # share, the coding agent and a CLI sub-agent (a shell with the net), her persona
    "confirm": {"on": True, "tools": ["shell__share:public", "code__start",
                                      "sub_agents__start:cli", "me__edit"]},
    "sites": {"on": True, "does": "watch"},
    "stops": {"on": True, "does": "freeze", "count": 3},
    # after a stop, the same thing again another way: `first` the first
    # try, `does` the next, within `minutes` of the stop
    "persist": {"on": True, "does": "freeze", "first": "watch", "minutes": 30},
    "injection": {"on": True},
    # escape is off on patrol: a finished turn's ways out read like its own work
    "patrol": {"on": True, "every_minutes": 90, "turns": 3, "rules": ["hijack", "damage"]},
    # the second look on an L1 yes (guard/chain.py); a provider that does not exist: none
    "chain": {"provider": "", "model": "", "seconds": 300,
              "thinking": "off"},
    # after a freeze (learn.py): "self" lifts it when the owner themselves unfroze at least
    # lift_after freezes of the same kind and kept none; "ask" always asks them
    "learn": {"lift": "self", "lift_after": 3},
    "gates": {c: {"public": "closed" if c == "sensitive" else "judge", "people": "judge",
                  "house": "open"} for c in GATED},
}


def plain(rule: str | None) -> str:
    """A rule in its words (`all`: every rule); a name that is no rule: a stopped step."""
    if rule == ALL:
        return ALL_WORDS
    return TABLE[rule].plain if rule in TABLE else "a step that was stopped"


def label(rule: str) -> str:
    """A rule as the owner's pages name it (`all`: every rule)."""
    if rule == ALL:
        return ALL_WORDS
    return TABLE[rule].label if rule in TABLE else rule


def escalates(rule: str, fired: set[str]) -> bool:
    """The hijack rule freezes when a rule of real harm fired on the same call or turn:
    an order from outside carried out that also leaks or destroys."""
    return rule == "hijack" and bool(fired & {"leak", "damage"})


def listed(tools: list[str], name: str, args: dict) -> bool:
    """Is the call on the confirm list: its tool, its skill, or "tool:arg" with that
    argument set (shell__share:public, sub_agents__start:cli)?"""
    for entry in tools:
        what, _, arg = entry.partition(":")
        if what in (name, name.split("__")[0]) and (not arg or (args or {}).get(arg)):
            return True
    return False


def gate(rules: dict, cls: str, sink: str) -> str:
    """The gate of class `cls` to `sink` in a rule set (current): open, judge, closed."""
    return rules["gates"].get(cls, {}).get(sink, "open")


def _gates(data: Any) -> dict[str, dict[str, str]]:
    if not isinstance(data, dict):
        raise Invalid("guard.gates", "This is a table of classes.")
    for cls, sinks in data.items():
        path = f"guard.gates.{cls}"
        if cls not in GATED:
            raise Invalid(path, f"'{cls}' is not a class with a gate. The classes: "
                          + ", ".join(GATED) + ".")
        if not isinstance(sinks, dict):
            raise Invalid(path, "This is a table of sinks.")
        for sink, v in sinks.items():
            if sink not in SINKS:
                raise Invalid(f"{path}.{sink}", f"'{sink}' is not a sink. The sinks: "
                              + ", ".join(SINKS) + ".")
            allowed = ("open", "closed") if sink == "house" else GATES
            if v not in allowed:
                raise Invalid(f"{path}.{sink}", "This must be one of " + ", ".join(allowed)
                              + ".")
    return {cls: dict(sinks) for cls, sinks in data.items()}


def _number(v, lo, hi) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and lo <= v <= hi


def _count(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, int) and v >= 1


_DOES = ("This must be one of " + ", ".join(DOES) + ".")
_COUNT = "This must be a whole number of at least 1."
_TEXT = "This must be text."
# every key of a table in DEFAULTS: (does the value fit, the sentence when it does not)
_KEYS: dict[str, tuple[Any, str]] = {
    "on": (lambda v: isinstance(v, bool), "This must be true or false."),
    "does": (lambda v: v in DOES, _DOES),
    "first": (lambda v: v in DOES, _DOES),
    "minutes": (_count, _COUNT),
    "count": (_count, _COUNT),
    "tools": (lambda v: isinstance(v, list) and all(isinstance(t, str) and t.strip()
                                                    for t in v),
              "This must be a list of tool or skill names."),
    "every_minutes": (_count, _COUNT),
    "turns": (_count, _COUNT),
    "rules": (lambda v: isinstance(v, list) and all(r in PATROLLED for r in v),
              "This must be a list of " + ", ".join(PATROLLED) + "."),
    "provider": (lambda v: isinstance(v, str), _TEXT),
    "model": (lambda v: isinstance(v, str), _TEXT),
    "seconds": (lambda v: _number(v, 10, 1800),
                "This must be a number of seconds from 10 to 1800."),
    "thinking": (valid_thinking, "This must be off or a thinking level, like low."),
    "lift": (lambda v: v in ("self", "ask"), "This must be self or ask."),
    "lift_after": (_count, _COUNT),
}


def validate(data: Any) -> dict[str, Any]:
    """The file as saved, checked: only known rules and keys, each of its type."""
    if not isinstance(data, dict):
        raise Invalid("guard", "The guard settings are a table.")
    out: dict[str, Any] = {}
    for name, rule in data.items():
        if name not in DEFAULTS:
            raise Invalid(f"guard.{name}", f"'{name}' is not a rule. The rules: "
                          + ", ".join(RULES) + ", [patrol], [chain], [learn] "
                          "and [gates].")
        if name == "gates":
            out[name] = _gates(rule)
            continue
        if not isinstance(rule, dict):
            raise Invalid(f"guard.{name}", "This is a table of keys.")
        for k, v in rule.items():
            path = f"guard.{name}.{k}"
            if k not in DEFAULTS[name]:
                raise Invalid(path, f"'{k}' is not a key of {name}. Its keys: "
                              + ", ".join(DEFAULTS[name]) + ".")
            fits, say = _KEYS[k]
            if not fits(v):
                raise Invalid(path, say)
        out[name] = dict(rule)
    return out


def current(cfg) -> dict[str, dict[str, Any]]:
    """The rule set now: the saved values over the defaults."""
    saved = getattr(getattr(cfg, "settings", None), "guard", None) or {}
    out = copy.deepcopy(DEFAULTS)
    for name, rule in saved.items():
        if name == "gates":
            for cls, sinks in rule.items():
                out[name][cls].update(sinks)
        else:
            out[name].update(rule)
    return out


async def probe(cfg, services) -> dict[str, str]:
    """Settings, Features (guard): the decision model (the `small` role's first model on a
    saved provider, guard/small_role.py) and the second look ([chain], guard/chain.py)."""
    from .chain import level2
    s = getattr(cfg, "settings", None)
    chain = [r for r in s.chain("small") if s.model(r) is not None] if s is not None else []
    if not chain:
        return {"state": "unset", "detail": "Pick a decision model below."}
    second = current(cfg)["chain"]
    detail = f"Decision model: {_name(s, chain[0])}."
    if not (second.get("provider") and second.get("model")):
        detail += " No second look."
    elif level2(cfg) is None:
        detail += f" Second look: {second['model']} is not on a saved provider."
    else:
        detail += f" Second look: {_name(s, second['provider'] + '/' + second['model'])}."
    return {"state": "ready", "detail": detail}


def _name(s, ref: str) -> str:
    """A model's label as the Models page shows it, else its id."""
    pid, _, model = ref.partition("/")
    m = (s.providers.get(pid) or {}).get("models", {}).get(model) or {}
    return m.get("label") or model
