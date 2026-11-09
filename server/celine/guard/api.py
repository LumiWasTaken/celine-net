"""Bastet over HTTP (docs/ARCHITECTURE.md, "Bastet"): her decisions, the freeze, her
texts (persona.py) and the owner's answers on its banners (the approvals' own routes are api/approvals.py).
Every route takes a logged-in session; a request with a token or a key, the kind
a script or its shellbox can hold, is refused (api/deps.py person_only). What changes
its freeze, a pass or an approval is done as the Owner the session makes (owner.py)."""
from __future__ import annotations

import copy

from fastapi import APIRouter, Header, Request, Response

from .. import reload
from ..api.deps import cfg_of, etag, json_body, person_only, services_of, settings_of
from ..errors import CelineError, NotFound
from ..settings import files, write
from ..settings.files import Invalid, version
from . import banners, freeze as freezes, owner, passes as passes_mod, persona, view
from . import prompts, questions, rules as rule_set, store as decisions
from .small_role import Unavailable

router = APIRouter(prefix="/api")


@router.get("/guard/decisions")
async def list_decisions(request: Request, decision: str | None = None,
                         source: str | None = None, turn: str | None = None,
                         rule: str | None = None, mode: str | None = None,
                         limit: int = 100, before: int | None = None) -> dict:
    """`decision`: one of store.DECISIONS; `source`: part of where the text came from;
    `turn`: one turn's; `rule`: one rule's (rules.RULES, gates); `mode`: watch or on.
    `next`: the `before` of the following page, null on the last one."""
    person_only(request)
    if decision is not None and decision not in decisions.DECISIONS:
        raise Invalid("decision", "This must be one of " + ", ".join(decisions.DECISIONS) + ".")
    if rule is not None and rule not in (*rule_set.RULES, "gates"):
        raise Invalid("rule", "This must be one of " + ", ".join(rule_set.RULES) + ", gates.")
    if mode is not None and mode not in ("watch", "on"):
        raise Invalid("mode", "This must be watch or on.")
    limit = max(1, min(limit, 500))
    rows = await decisions.listing(services_of(request).store, decision=decision,
                                   source=source, turn=turn, rule=rule, mode=mode,
                                   limit=limit + 1, before=before)
    more = len(rows) > limit
    rows = rows[:limit]
    return {"decisions": rows, "next": rows[-1]["id"] if more else None}


@router.get("/guard/decisions/{rid}")
async def get_decision(rid: int, request: Request) -> dict:
    """One decision whole, for its detail view: the row (p is the score, model, ms,
    quote, why, piece, ...), the call's `arguments`, Bastet's `line`, `kind` and
    `actions` (store.one), the `question` the judge was asked
    (questions.toml; null for hijack, whose frame is prompts/hijack.md, and for a rule
    code decided alone), the `prompt` file that framed it, and its `banner` (null when
    it shows none)."""
    person_only(request)
    services = services_of(request)
    row = await decisions.one(services.store, rid)
    if row is None:
        raise NotFound("decision")
    table = decisions.table_of(row)
    asked = prompts.questions()[table]
    row["question"] = questions.asked(services.cfg, [row["rule"]], table)[row["rule"]] \
        if row["rule"] in asked and row["layer"] == 2 else None
    row["prompt"] = "hijack" if row["rule"] == "hijack" else "patrol" \
        if row["does"] == "patrol" else "page" if row["rule"] == "injection" else \
        "question" if row["question"] else None
    row["banner"] = await services.store.read(lambda c: decisions.banner(c, rid))
    row["chain"] = decisions.levels(row)
    return {"decision": row}


# ---- the freeze -------------------------------------------------------------------------

@router.get("/guard/freeze")
async def get_frozen(request: Request) -> dict:
    """{"frozen": the open freeze or null}."""
    person_only(request)
    return {"frozen": await freezes.frozen(services_of(request).store)}


@router.post("/guard/freeze")
async def post_freeze(request: Request) -> dict:
    """{"why": "..."}: the owner freezes it. Idempotent. -> {"frozen": the freeze}."""
    person_only(request)
    body = await json_body(request, allow=("why",))
    why = body.get("why") or "Frozen by hand."
    if not isinstance(why, str):
        raise Invalid("why", "This must be text.")
    return {"frozen": await freezes.freeze(services_of(request), by="user", why=why)}


@router.delete("/guard/freeze")
async def post_unfreeze(request: Request) -> dict:
    """The agent goes on. Idempotent. -> {"unfrozen": the freeze that ended, or null}."""
    by = owner.session(request)
    return {"unfrozen": await freezes.unfreeze(services_of(request), by=by)}


# ---- its texts: persona, talk, note ------------------------------------------------------

@router.get("/guard/texts")
async def get_texts(request: Request) -> dict:
    """{"texts": {persona, talk, note: {text, default, own}}} (persona.py)."""
    person_only(request)
    return {"texts": await services_of(request).store.read(persona.view)}


@router.put("/guard/texts/{field}")
async def put_text(field: str, request: Request) -> dict:
    """{"text": the text, or null for the default}: saved with its History. -> as GET;
    the event `guard.texts.changed` tells every page."""
    person_only(request)
    body = await json_body(request, need=("text",), allow=("text",))
    store = services_of(request).store
    await store.write(lambda c: persona.write(c, field, body["text"]))
    store.emit_global({"type": "guard.texts.changed"})
    return {"texts": await store.read(persona.view)}


# ---- the banners in the chat -------------------------------------------------------------

@router.post("/guard/banners/{rid}/replies")
async def answer_banner(rid: int, request: Request) -> dict:
    """The owner answers Bastet in a banner's thread: {"text": their words} (read by the
    guard role) or {"button": banners.BUTTONS}. -> {"banner": the banner, its answer
    last in its thread}. 503 bastet_away when the reader cannot answer now."""
    by = owner.session(request)
    body = await json_body(request, allow=("text", "button"))
    text, button = body.get("text"), body.get("button")
    if (text is None) == (button is None):
        raise Invalid("text", "Send either text or a button.")
    if button is not None and button not in banners.BUTTONS:
        raise Invalid("button", "This must be one of " + ", ".join(banners.BUTTONS) + ".")
    if text is not None and (not isinstance(text, str) or not text.strip()):
        raise Invalid("text", "This must be text.")
    try:
        got = await banners.answer(services_of(request), rid, by=by, text=text, button=button)
    except KeyError:
        raise NotFound("banner") from None
    except Unavailable as e:
        raise CelineError("bastet_away", f"Bastet could not read your answer: {e}",
                          503) from None
    return {"banner": got}


# ---- passes -------------------------------------------------------------------------------

@router.get("/guard/passes")
async def list_passes(request: Request) -> dict:
    """{"passes": every pass that holds now, each with its rule's `label` and a `line`
    saying where and until when (passes.shown)}."""
    person_only(request)
    services = services_of(request)
    return {"passes": await passes_mod.shown(services.store, services.cfg)}


@router.delete("/guard/passes/{pid}")
async def end_pass(pid: int, request: Request) -> dict:
    """The pass ends now (the event guard.passes). 404 when no such pass holds."""
    person_only(request)
    if not await passes_mod.end(services_of(request).store, pid):
        raise NotFound("pass")
    return {"ended": pid}


# ---- its rules (var/settings/guard.toml) -----------------------------------------------

def _rules_view(request: Request) -> dict:
    s, cfg = settings_of(request), cfg_of(request)
    current = rule_set.current(cfg)
    return {**current, "version": version(s.guard), "saved": bool(s.saved.get("guard")),
            "rows": rule_set.rows(current), "acts": rule_set.ACTS,
            "help": {k: cfg.substitute(v) for k, v in view.PAGE_HELP.items()}}


@router.get("/guard/rules")
async def get_guard_rules(request: Request, response: Response) -> dict:
    """The rules in use: the saved values over guard/rules.py DEFAULTS (model, chain,
    patrol, learn, gates and the rule tables), plus version and saved; `rows` (each rule
    Settings, Guard sets: its label and what it does now, rules.rows), `acts` (the words
    of what a rule can do) and `help` (the page's "?" texts, names filled in)."""
    person_only(request)
    view = _rules_view(request)
    etag(response, view["version"])
    return view


@router.put("/guard/rules")
async def put_guard_rules(request: Request, response: Response,
                          if_match: str | None = Header(default=None)) -> dict:
    """The tables to change, merged into the saved rules: {"chain": {provider, model}}
    changes only those two keys. Validated as the file is (guard/rules.py)."""
    person_only(request)
    body = await json_body(request)
    body.pop("version", None)
    body.pop("saved", None)
    async with write.lock():
        s = settings_of(request)
        files.check_version("guard", s.guard, if_match)
        given = rule_set.validate(body)
        merged = copy.deepcopy(s.guard)
        for name, table in given.items():
            if name == "gates":
                for cls, sinks in table.items():
                    merged.setdefault("gates", {}).setdefault(cls, {}).update(sinks)
            else:
                merged.setdefault(name, {}).update(table)
        out = await reload.commit(services_of(request), {"guard": merged})
        view = _rules_view(request)
        etag(response, view["version"])
        return {**view, **out}
