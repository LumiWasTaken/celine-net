"""Features: what of Celine is switched on (var/settings/features.toml, Settings,
Features). While no file is saved, what needs a service (`needs_service`: the boxes,
ComfyUI, SearXNG, the connectors, email, the vault) is off and the rest on (`seed`);
once saved, every feature is on until the file says otherwise, but one with `start_on`
False (shares, guard), which is on only once the file says so. `done` is False until a
PUT says the onboarding finished (config status `setup`).

Three tiers (TIERS): core, what she is made of, with no switch (CORE: the chat and each
core skill of skills.CORE that no feature owns); base, what ships in the repo (BASE, and
the plugins in plugins/) or has a place of its own; user, a plugin from <data>/plugins
with no place, whose removal leaves at most its data behind.

The plug: BASE declares each base feature once, and each plugin is one more
(`catalog()`, from its plugin.toml: celine/plugins.py). Adding a base module is one
Feature here and one `probe(cfg, services)` beside the module. The skills, the MCP servers, the archives,
the UI places and Settings pages, `celine doctor` and the onboarding all follow.

A feature off: its skills are not built (config.skill_enabled, skills_off), nor its tools
of another feature's skill (`tools`, Registry.add_specs), its MCP server is not started
and its archive not opened (discord, whatsapp, documents), its service is not followed
(shellbox, browser, vault), Bastet is off (guard). All of it hot: settings commit() and
reload.apply rebuild the skills, and the lifespan's runners (core/runners.py) start and
stop what runs for a feature on a features or services PUT.
"""
from __future__ import annotations

import logging

import importlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import anyio

from .. import plugins
from .files import Invalid

log = logging.getLogger("celine.features")
# CELINE_DEMO=1 (tools/testdata/demo.sh): every feature answers ready and nothing outside is
# asked, so an instance on invented data shows every place as set up
DEMO = os.environ.get("CELINE_DEMO") == "1"

PROBE_SECONDS = 3.0        # one probe's cap
CACHE_SECONDS = 10.0       # probes answer from the cache this long
STATES = ("ready", "missing", "unset", "none")


@dataclass(frozen=True)
class Feature:
    id: str
    label: str
    needs: str = ""                  # the disabled switch's tooltip: what turns it on
    about: str = ""                  # one sentence: what it gives the user
    off_when: str = ""               # one sentence: "Turn it off if ...", the switch's tooltip
    skills: tuple[str, ...] = ()     # skill ids it owns: off while the feature is off
    tools: tuple[str, ...] = ()      # tool wire names of a skill it does not own: off with it
    places: tuple[str, ...] = ()     # UI place ids (web/src/routes.tsx)
    pages: tuple[str, ...] = ()      # Settings page ids it owns
    mcp: tuple[str, ...] = ()        # MCP server ids: mcp.toml's, a plugin's [servers]
    kbs: tuple[str, ...] = ()        # kb.toml source ids
    probe: str = ""                  # "celine.module:function", async (cfg, services) -> Probe
    setup: str = ""                  # the onboarding's setup form for it
    form: str = ""                   # its form at /api/forms/<id> (Settings, the onboarding)
    # a connector's archive feature (discord, whatsapp: "messages"): its onboarding step
    # offers that switch and the first import once the connection is linked
    archive: str = ""
    # on only once saved on: never on by itself, not on a fresh install nor for an id left out
    start_on: bool = True
    # on only while this feature is on: its switch is off and greyed while that one is off
    after: str = ""
    # the confirm a switch on shows first (Settings, Features and the onboarding)
    warn: str = ""
    # the onboarding step it shares with the other features of its group (GROUPS), and
    # its sub-heading there
    group: str = ""
    choice: str = ""
    tier: str = "base"               # "base" or "user" (TIERS)


# the onboarding's shared steps: id -> its title (cfg.substitute fills the name)
GROUPS = {"computer": "{{name}}'s computer"}

# the tiers in their order: id -> its heading
TIERS = {"core": "Core", "base": "Base modules", "user": "User modules"}

# The core, with no switch: the chat, and each core skill no feature owns (skills.CORE; a
# test holds them equal). id, label, about (cfg.substitute fills the names).
CORE: tuple[tuple[str, str, str], ...] = (
    ("chat", "Chat", "Conversations with {{name}}, with branches, files and pictures."),
    ("notebook", "Notebook", "{{Her}} own notes, kept as files and carried into every conversation."),
    ("mind", "Mind", "{{Her}} mood and thoughts between conversations."),
    ("plan", "Plan", "The steps of a bigger job, pinned at the top of its conversation."),
    ("intercom", "Intercom", "Messages between you, {{name}}, missions and sub-agents, and reminders."),
    ("past_conversations", "Past conversations", "Your earlier conversations, searched by word."),
    ("bugreport", "Bug reports", "Problems {{she}} finds in the app, sent to you on the intercom."),
    ("skills", "Skills", "The guide to each tool, read before {{she}} uses it."),
)


# The order is the UI's; the plugins come after.
BASE: tuple[Feature, ...] = (
    Feature("shellbox", "Shellbox", "Turns on once the shellbox answers.",
            "An isolated Linux computer with a terminal, files and programs of its own, nothing on your machine.",
            "Turn it off if you don't run the shellbox container.",
            skills=("shell", "code"), places=("terminals",), probe="celine.skills.shell:probe",
            setup="service", group="computer", choice="Container"),
    Feature("shares", "Shares", "Turns on once the shellbox is on.",
            "Shows you a page, game, tool or file from the shellbox in a new tab.",
            "Turn it off if nothing from the shellbox should open in a tab.",
            tools=("shell__share", "shell__unshare", "shell__list_shares"),
            probe="celine.shares:probe", start_on=False, after="shellbox",
            warn="The link of a share is its key. A page opens only for you while you are "
                 "logged in, but anyone who gets a link can reach what it shares with a "
                 "script until it ends (24 hours, or what you set) or you stop it. A public "
                 "share opens for anyone."),
    Feature("browser", "Browser", "Turns on once the browser answers.",
            "An isolated Brave browser with ad blocking: pages, logins and forms, with a history you can review and a live view you can watch.",
            "Turn it off if you don't run the browser container.",
            skills=("browser",), places=("browser",), probe="celine.browser.sync:probe",
            setup="service"),
    Feature("images", "Images", "Turns on once ComfyUI answers.",
            "Pictures made with ComfyUI in the styles of the presets you keep.",
            "Turn it off if you have no ComfyUI, or no GPU time for pictures.",
            skills=("image_generator",), probe="celine.comfyui.server:probe_images",
            pages=("media",), setup="comfyui"),
    Feature("music", "Music", "Turns on once ComfyUI answers.",
            "Music made with ComfyUI from a few words about the track.",
            "Turn it off if you have no ComfyUI, or no GPU time for music.",
            skills=("music_generator",), probe="celine.comfyui.server:probe_music",
            pages=("media",), setup="comfyui"),
    Feature("speech_input", "Dictation", "Pick a Dictation model first.",
            "Speak a message, read it over, then send it.",
            "Turn it off if you only want to type.",
            probe="celine.speech:probe_input", setup="speech", start_on=False),
    Feature("speech_output", "Read aloud", "Pick a Read aloud model first.",
            "Replies read out loud, in a voice you pick.",
            "Turn it off if replies should stay text.",
            probe="celine.speech:probe_output", setup="speech", start_on=False),
    Feature("web", "Web search", "Turns on once SearXNG answers.",
            "Web search through your own SearXNG, with the pages found read as text or in the browser.",
            "Turn it off if nothing should be looked up on the internet.",
            skills=("web",), probe="celine.skills.web:probe", setup="address"),
    Feature("discord", "Discord", "Turns on once the Discord connector answers.",
            "Your Discord history in the archive, and a chat from Discord itself.",
            "Turn it off if you don't use Discord.",
            pages=("source:discord",), mcp=("discord",), kbs=("discord",),
            probe="celine.mcp:probe_discord", archive="messages"),
    Feature("whatsapp", "WhatsApp", "Turns on once the WhatsApp connector answers.",
            "Your WhatsApp history in the archive, and a chat from WhatsApp itself.",
            "Turn it off if you don't use WhatsApp.",
            pages=("source:whatsapp",), mcp=("whatsapp",), kbs=("whatsapp",),
            probe="celine.mcp:probe_whatsapp", archive="messages"),
    Feature("email", "Email", "Turns on once the account is filled in.",
            "A mailbox of yours read on your behalf: what came in, searchable and summarised.",
            "Turn it off if your mail should stay out of reach.",
            skills=("email",), pages=("email",), probe="celine.skills.mail:probe",
            setup="email"),
    Feature("vault", "Vault", "Turns on once the Vaultwarden details are filled in.",
            "A Vaultwarden collection with the logins used in the browser on your behalf.",
            "Turn it off if you have no Vaultwarden, or no password should be stored.",
            skills=("vault",), pages=("vault",), probe="celine.vault:probe", setup="vault"),
    Feature("guard", "Guard", "Turns on once a decision model is picked.",
            "Bastet, a second model that checks risky actions before they run and asks you "
            "when unsure.",
            "Turn it off if you want no second check before risky actions.",
            probe="celine.guard.rules:probe", setup="guard", start_on=False),
    Feature("documents", "Documents", "",
            "A folder of your documents, searched, read and cited.",
            "Turn it off if you have no documents to share.",
            skills=("user_documents",), places=("documents",), kbs=("documents",)),
    Feature("messages", "Messages", "Turns on once Discord or WhatsApp is connected.",
            "Your chat history in one archive, searched by word or by topic.",
            "Turn it off if you have no Discord or WhatsApp connected.",
            skills=("chat_archive",), places=("chats", "search"), pages=("people", "search"),
            probe="celine.kb:probe", setup="search"),
    Feature("artifacts", "Artifacts", "",
            "Built pages: a chart, a table, a small app, kept with every version.",
            "Turn it off if replies should only be text.",
            skills=("artifacts",)),
    Feature("routines", "Routines", "",
            "Jobs on a schedule, a morning digest or a nightly tidy of the notes; change a time or switch one off.",
            "Turn it off if nothing should run on a schedule, only when you write.",
            skills=("routines",), places=("routines",), setup="routines"),
    Feature("missions", "Missions", "",
            "A long job carried over days, with check-ins as it goes.",
            "Turn it off if you only ask short questions.",
            skills=("missions",), places=("missions",)),
    Feature("sub_agents", "Sub-agents", "",
            "Parts of a big job handed to helpers, their results collected.",
            "Turn it off if your model server handles only one conversation at a time.",
            skills=("sub_agents",)),
)


def of_plugin(p: plugins.Plugin) -> Feature:
    """A plugin's switch, from its plugin.toml; off until saved on unless it says
    `start_on = true`."""
    m = p.manifest
    return Feature(p.id, m["label"], m.get("needs", ""), m.get("about", ""),
                   m.get("off_when", ""), skills=tuple(p.skills),
                   places=tuple(m.get("places", ())),
                   pages=(p.id,) if p.form is not None else (),
                   form=p.id if p.form is not None else "",
                   start_on=bool(m.get("start_on", False)),
                   warn=m.get("warn", ""), mcp=tuple(m.get("servers", ())),
                   group=m.get("group", "") if m.get("group") in GROUPS else "",
                   choice=m.get("choice", ""),
                   tier="user" if p.user and not m.get("places") else "base")


def catalog() -> tuple[Feature, ...]:
    """BASE then the plugins, by tier; a group's features together at its first one's place."""
    items = BASE + tuple(of_plugin(p) for p in plugins.all())
    tiers = list(TIERS)
    first = {f.group: i for i, f in reversed(list(enumerate(items))) if f.group}
    order = sorted(range(len(items)), key=lambda i: (
        tiers.index(items[i].tier), first.get(items[i].group, i), i))
    return tuple(items[i] for i in order)


def get(fid: str) -> Feature:
    return next(f for f in catalog() if f.id == fid)


def ids() -> tuple[str, ...]:
    return tuple(f.id for f in catalog())


def skill_owners() -> dict[str, str]:
    """skill id -> its feature"""
    return {s: f.id for f in catalog() for s in f.skills}


def tool_owners() -> dict[str, str]:
    """tool wire name -> its feature"""
    return {t: f.id for f in catalog() for t in f.tools}


def connector_owners() -> dict[str, str]:
    """MCP server or kb id -> its feature"""
    return {c: f.id for f in catalog() for c in f.mcp + f.kbs}


def needs_service(f: Feature) -> bool:
    """Something outside the app must answer first: off until the onboarding saved it."""
    return bool(f.mcp) or f.setup in ("service", "comfyui", "address", "email", "vault")


def seed() -> dict[str, bool]:
    """The section while features.toml is unsaved: nothing that needs a service starts,
    nor what starts off."""
    return {f.id: False for f in catalog() if needs_service(f) or not f.start_on}


def view(f: Feature, fill=lambda text: text) -> dict[str, Any]:
    """The static shape the client gets (no probe); `fill` puts its name and pronouns in
    (cfg.substitute)."""
    return {"id": f.id, "label": fill(f.label), "needs": fill(f.needs), "about": fill(f.about),
            "off_when": fill(f.off_when), "places": list(f.places), "pages": list(f.pages),
            "setup": f.setup, "form": f.form, "kbs": list(f.kbs), "archive": f.archive,
            "warn": fill(f.warn), "after": f.after, "group": f.group, "choice": fill(f.choice), "tier": f.tier,
            "step": has_step(f)}


def has_step(f: Feature) -> bool:
    """The onboarding gives it a setup step while it is on: a form, or a connection."""
    return bool(f.setup or f.form or f.archive)


def validate(data: Any) -> dict[str, bool]:
    if not isinstance(data, dict):
        raise Invalid("features", "Features are an object.")
    out: dict[str, bool] = {}
    known = ids()
    for k, v in data.items():
        if k not in known and k != "done":
            raise Invalid(f"features.{k}", f"'{k}' is not a feature.")
        if not isinstance(v, bool):
            raise Invalid(f"features.{k}", "This must be true or false.")
        out[k] = v
    return out


def effective(saved: dict[str, bool] | None) -> dict[str, bool]:
    """Every feature, a missing one on unless it starts off; one `after` a feature that
    is off is off."""
    out = {f.id: (saved or {}).get(f.id, f.start_on) for f in catalog()}
    return {f.id: out[f.id] and (not f.after or out[f.after]) for f in catalog()}


def done(saved: dict[str, bool] | None) -> bool:
    """The onboarding finished: a PUT said `done` once."""
    return bool((saved or {}).get("done", False))


def on(cfg, feature: str) -> bool:
    """Is the feature on, as saved now? A cfg without settings has everything on."""
    s = getattr(cfg, "settings", None)
    if s is None:
        return True
    return effective(getattr(s, "features", None))[feature]


def skills_off(features: dict[str, bool] | None) -> frozenset[str]:
    """The skills whose feature is off (config.skill_enabled)."""
    on = effective(features)
    return frozenset(s for s, f in skill_owners().items() if not on[f])


def tool_on(cfg, wire_name: str) -> bool:
    """False for a tool whose feature is off (`tools`): it is not built."""
    f = tool_owners().get(wire_name)
    return f is None or on(cfg, f)


def allows_connector(cfg, server_or_kb_id: str) -> bool:
    f = connector_owners().get(server_or_kb_id)
    return f is None or on(cfg, f)


# ---- probes ---------------------------------------------------------------------------

def probe_fn(f: Feature):
    """The probe beside the module, resolved when asked (so a test may replace it)."""
    if not f.probe:
        return None
    module, _, name = f.probe.partition(":")
    return getattr(importlib.import_module(module), name)


def shown(p: dict[str, Any]) -> dict[str, Any]:
    """A probe as the client gets it, with "blocks": whether its switch cannot be on
    (missing or unset, but a plugin waiting for its own form: _plugin_state)."""
    return {**p, "blocks": p.get("blocks", p["state"] in ("missing", "unset"))}


async def probe(cfg, services, feature: str) -> dict[str, Any]:
    """One feature's state now: {"state", "detail"}, with "url" when the probe found the
    service at its default address while none is saved (the setup form prefills it), and
    "why" when what she reads differs from the page's words (errors.her_*).
    "none" when it has nothing to detect; a probe that does not answer within
    PROBE_SECONDS, or raises, is "missing"."""
    f = get(feature)
    fn = probe_fn(f)
    if fn is None:
        return _plugin_state(cfg, feature)
    if DEMO:
        return {"state": "ready", "detail": "Demo"}
    try:
        with anyio.fail_after(PROBE_SECONDS):
            result = await fn(cfg, services)
    except TimeoutError:
        return {"state": "missing", "detail": f"{f.label} did not answer in time."}
    except Exception as e:                       # a probe never takes the page down
        log.warning("feature probe failed", extra={"event": "features.probe_failed",
                                                   "feature": feature,
                                                   "error": type(e).__name__})
        return {"state": "missing",
                "detail": f"{f.label} could not be checked ({type(e).__name__})."}
    if result is None:
        return {"state": "none", "detail": ""}
    # the reason its Dot shows: what the probe said, else what turns it on
    detail = result.get("detail") or (f.needs if result["state"] in ("missing", "unset") else "")
    out = {"state": result["state"], "detail": detail}
    # why: what she reads (me__whoami), when it differs; key: the box refused the app's key
    for key in ("url", "why", "key"):
        if result.get(key):
            out[key] = result[key]
    if "blocks" in result:              # a probe whose state never blocks its switch
        out["blocks"] = result["blocks"]
    return out


def _plugin_state(cfg, feature: str) -> dict[str, Any]:
    """A feature without a probe: "unset" for a plugin while a value its servers need
    (a `${KEY}` of its form in its [servers] blocks, form.fill) is empty, with what is
    empty; "ready" once all are filled; "none" without servers. It never blocks the switch: its form is filled from what runs
    only while it is on (its setup line, its routes)."""
    from . import form
    p = plugins.get(feature)
    if p is None or not p.manifest.get("servers"):
        return {"state": "none", "detail": ""}
    empty: list[str] = []
    form.fill(cfg, p.id, (p.form or {}).get("fields", []),
              json.dumps(p.manifest["servers"]), empty)
    if not empty:
        return {"state": "ready", "detail": ""}
    return {"state": "unset", "detail": "; ".join(dict.fromkeys(empty)) + ".",
            "blocks": False}


async def probe_all(cfg, services, *, cache: bool = True) -> dict[str, dict[str, str]]:
    """Every feature's state, concurrently, from the cache in the services bag while it is
    younger than CACHE_SECONDS. `services` None (celine doctor): no cache."""
    hit = getattr(services, "feature_probes", None)
    if cache and hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    out: dict[str, dict[str, str]] = {}

    async def one(f: Feature) -> None:
        out[f.id] = await probe(cfg, services, f.id)

    async with anyio.create_task_group() as tg:
        for f in catalog():
            tg.start_soon(one, f)
    out = {f.id: out[f.id] for f in catalog()}
    if services is not None:
        services.feature_probes = (time.monotonic(), out)
    return out
