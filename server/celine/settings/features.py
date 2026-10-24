"""Features: what of Celine is switched on (var/settings/features.toml, Settings,
Features). While no file is saved, what needs a service (`needs_service`: the boxes,
ComfyUI, SearXNG, the connectors, email, the vault) is off and the rest on (`seed`);
once saved, every feature is on until the file says otherwise, but one with `start_on`
False (shares, guard), which is on only once the file says so. `done` is False until a
PUT says the onboarding finished (config status `setup`).

The plug: CATALOG declares each feature once. Adding a module is one Feature here and
one `probe(cfg, services)` beside the module. The skills, the MCP servers, the archives,
the UI places and Settings pages, `celine doctor` and the onboarding all follow.

A feature off: its skills are not built (config.skill_enabled, skills_off), nor its tools
of another feature's skill (`tools`, Registry.add_specs), its MCP server
is not started and its archive not opened (discord, whatsapp), its service is not followed
(shellbox, browser, vault), Bastet is off (guard). All of it hot: settings commit() and
reload.apply rebuild the skills, and the lifespan's runners (core/runners.py) start and
stop what runs for a feature on a features or services PUT.
"""
from __future__ import annotations

import logging

import importlib
import time
from dataclasses import dataclass
from typing import Any

import anyio

from .files import Invalid

log = logging.getLogger("celine.features")

PROBE_SECONDS = 3.0        # one probe's cap
CACHE_SECONDS = 10.0       # probes answer from the cache this long
STATES = ("ready", "missing", "unset", "none")


@dataclass(frozen=True)
class Feature:
    id: str
    label: str
    needs: str = ""                  # the disabled switch's tooltip: what turns it on
    about: str = ""                  # one sentence: what it gives the user
    off_when: str = ""               # one short phrase: why someone turns it off
    skills: tuple[str, ...] = ()     # skill ids it owns: off while the feature is off
    tools: tuple[str, ...] = ()      # tool wire names of a skill it does not own: off with it
    places: tuple[str, ...] = ()     # UI place ids (web/src/routes.tsx)
    pages: tuple[str, ...] = ()      # Settings page ids it owns
    mcp: tuple[str, ...] = ()        # mcp.toml server ids
    kbs: tuple[str, ...] = ()        # kb.toml source ids
    probe: str = ""                  # "celine.module:function", async (cfg, services) -> Probe
    setup: str = ""                  # the onboarding's setup form for it
    # the onboarding's card: "detect" when its service answered (ready) or has an address
    # and did not (missing); "always" (the guard, routines, and the connectors, whose card
    # says how to run them); "" never (email, vault: the Later card and Settings)
    card: str = ""
    # a connector's archive feature (discord, whatsapp: "messages"): its onboarding card
    # offers that switch and the first import once the connection is linked
    archive: str = ""
    # on only once saved on: never on by itself, not on a fresh install nor for an id left out
    start_on: bool = True
    # the confirm a switch on shows first (Settings, Features and its onboarding card)
    warn: str = ""


# The order is the UI's.
CATALOG: tuple[Feature, ...] = (
    Feature("shellbox", "Shellbox", "Turns on once the shellbox answers.",
            "An isolated Linux computer with a terminal, files and programs of its own, nothing on your machine.",
            "No shellbox container is running.",
            skills=("shell", "code"), places=("terminals",), probe="celine.skills.shell:probe",
            setup="service", card="detect"),
    Feature("shares", "Shares", "Turns on once the shellbox is on.",
            "Shows you a page, game, tool or file from the shellbox in a new tab.",
            "Nothing of the shellbox should open outside it.",
            tools=("shell__share", "shell__unshare", "shell__list_shares"),
            probe="celine.shares:probe", card="always", start_on=False,
            warn="The link of a share is its key. A page opens only for you while you are "
                 "logged in, but anyone who gets a link can reach what it shares with a "
                 "script until it ends (24 hours, or what you set) or you stop it. A public "
                 "share opens for anyone."),
    Feature("browser", "Browser", "Turns on once the browser answers.",
            "An isolated Brave browser with ad blocking: pages, logins and forms, with a history you can review and a live view you can watch.",
            "No browser container is running.",
            skills=("browser",), places=("browser",), probe="celine.browser.sync:probe",
            setup="service", card="detect"),
    Feature("images", "Images", "Turns on once ComfyUI answers.",
            "Pictures made with ComfyUI in the styles of the presets you keep.",
            "No ComfyUI, or no GPU time for pictures.",
            skills=("image_generator",), probe="celine.comfyui.server:probe_images",
            pages=("media",), setup="comfyui", card="detect"),
    Feature("music", "Music", "Turns on once ComfyUI answers.",
            "Music made with ComfyUI from a few words about the track.",
            "No ComfyUI, or no GPU time for music.",
            skills=("music_generator",), probe="celine.comfyui.server:probe_music",
             pages=("media",), setup="comfyui", card="detect"),
    Feature("speech_input", "Dictation", "Add a speech-to-text provider first.",
            "Record a message and review its transcript before sending.",
            "No microphone or speech provider wanted.",
            probe="celine.settings.speech:probe_input", setup="speech", card="always", start_on=False),
    Feature("speech_output", "Read aloud", "Add a text-to-speech provider first.",
            "Listen to completed replies using your speech provider.",
            "Text only.",
            probe="celine.settings.speech:probe_output", setup="speech", card="always", start_on=False),
    Feature("web", "Web search", "Turns on once SearXNG answers.",
            "Web search through your own SearXNG, with the pages found read as text or in the browser.",
            "Only what is on this machine should be known.",
            skills=("web",), probe="celine.skills.web:probe", setup="address", card="detect"),
    Feature("discord", "Discord", "Turns on once the Discord connector answers.",
            "Your Discord history in the archive, and a chat from Discord itself.",
            "You do not use Discord.",
            pages=("source:discord",), mcp=("discord",), kbs=("discord",),
            probe="celine.mcp:probe_discord", card="always", archive="messages"),
    Feature("whatsapp", "WhatsApp", "Turns on once the WhatsApp connector answers.",
            "Your WhatsApp history in the archive, and a chat from WhatsApp itself.",
            "You do not use WhatsApp.",
            pages=("source:whatsapp",), mcp=("whatsapp",), kbs=("whatsapp",),
            probe="celine.mcp:probe_whatsapp", card="always", archive="messages"),
    Feature("email", "Email", "Turns on once the account is filled in.",
            "A mailbox of yours read on your behalf: what came in, searchable and summarised.",
            "Mail should stay out of reach.",
            skills=("email",), pages=("email",), probe="celine.skills.mail:probe",
            setup="email"),
    Feature("vault", "Vault", "Turns on once the Vaultwarden details are filled in.",
            "A Vaultwarden collection with the logins used in the browser on your behalf.",
            "No Vaultwarden, or no password should be held at all.",
            skills=("vault",), pages=("vault",), probe="celine.vault:probe", setup="vault"),
    Feature("guard", "Guard", "Turns on once a decision model is picked.",
            "Bastet, a second model that checks risky actions before they run and asks you "
            "when unsure.",
            "No second opinion wanted on tool use.",
            probe="celine.guard.rules:probe", setup="guard", card="always", start_on=False),
    Feature("documents", "Documents", "",
            "A folder of your documents, searched, read and cited.",
            "No documents to give.",
            skills=("user_documents",), places=("documents",)),
    Feature("messages", "Messages", "Turns on once Discord or WhatsApp is connected.",
            "Your chat history in one archive, searched by word or by topic.",
            "No Discord or WhatsApp connected.",
            skills=("chat_archive",), places=("chats", "search"), pages=("people", "search"),
            probe="celine.kb:probe", setup="search", card="detect"),
    Feature("artifacts", "Artifacts", "",
            "Built pages: a chart, a table, a small app, kept with every version.",
            "Text only.",
            skills=("artifacts",)),
    Feature("routines", "Routines", "",
            "Jobs on a schedule, a morning digest or a nightly tidy of the notes; change a time or switch one off.",
            "Only on demand, nothing on a schedule.",
            skills=("routines",), places=("routines",), setup="routines", card="always"),
    Feature("missions", "Missions", "",
            "A long job carried over days, with check-ins as it goes.",
            "Short questions only.",
            skills=("missions",), places=("missions",)),
    Feature("sub_agents", "Sub-agents", "",
            "Parts of a big job handed to helpers, their results collected.",
            "One conversation at a time on a small model server.",
            skills=("sub_agents",)),
)
BY_ID = {f.id: f for f in CATALOG}


def needs_service(f: Feature) -> bool:
    """Something outside the app must answer first: off until the onboarding saved it."""
    return bool(f.mcp) or f.setup in ("service", "comfyui", "address", "email", "vault")


def seed() -> dict[str, bool]:
    """The section while features.toml is unsaved: nothing that needs a service starts,
    nor what starts off."""
    return {f.id: False for f in CATALOG if needs_service(f) or not f.start_on}


IDS = tuple(BY_ID)
SKILLS = {s: f.id for f in CATALOG for s in f.skills}          # skill id -> feature
TOOLS = {t: f.id for f in CATALOG for t in f.tools}            # tool wire name -> feature
CONNECTORS = {c: f.id for f in CATALOG for c in f.mcp + f.kbs}  # server or kb id -> feature


def view(f: Feature) -> dict[str, Any]:
    """The static shape the client gets (no probe)."""
    return {"id": f.id, "label": f.label, "needs": f.needs, "about": f.about,
            "off_when": f.off_when, "places": list(f.places), "pages": list(f.pages),
            "setup": f.setup, "card": f.card, "kbs": list(f.kbs), "archive": f.archive,
            "warn": f.warn}


# which saved thing means "the user gave this feature an address" (a card even when it
# does not answer); a feature not here gets a card only when its probe is ready
ADDRESSED: dict[str, str] = {"shellbox": "shellbox", "browser": "browser", "web": "searxng"}


def card_now(cfg, f: Feature, probe: dict[str, str]) -> str:
    """The onboarding's card policy for what the probe found now: "always"; "detect" when
    the service answers, or when it has an address (a saved services url from the user, a
    saved ComfyUI provider, the default address a probe found something at: `url`) and it
    does not answer; "" otherwise (Later)."""
    if f.card != "detect":
        return f.card
    state = probe["state"]
    if state == "ready" or (state == "missing" and probe.get("url")):
        return "detect"
    if state != "missing":
        return ""
    s = getattr(cfg, "settings", None)
    if s is None:
        return ""
    if f.id in ADDRESSED:
        saved = (s.saved or {}).get("services", False)
        return "detect" if saved and (s.services.get(ADDRESSED[f.id]) or {}).get("url") else ""
    if f.id in ("images", "music"):
        return "detect" if any(p.get("kind") == "comfyui" for p in s.providers.values()) else ""
    return ""


def validate(data: Any) -> dict[str, bool]:
    if not isinstance(data, dict):
        raise Invalid("features", "Features are an object.")
    out: dict[str, bool] = {}
    for k, v in data.items():
        if k not in IDS and k != "done":
            raise Invalid(f"features.{k}", f"'{k}' is not a feature.")
        if not isinstance(v, bool):
            raise Invalid(f"features.{k}", "This must be true or false.")
        out[k] = v
    return out


def effective(saved: dict[str, bool] | None) -> dict[str, bool]:
    """Every feature, a missing one on unless it starts off."""
    return {f.id: (saved or {}).get(f.id, f.start_on) for f in CATALOG}


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
    return frozenset(s for s, f in SKILLS.items() if not on[f])


def tool_on(cfg, wire_name: str) -> bool:
    """False for a tool whose feature is off (`tools`): it is not built."""
    f = TOOLS.get(wire_name)
    return f is None or on(cfg, f)


def allows_connector(cfg, server_or_kb_id: str) -> bool:
    f = CONNECTORS.get(server_or_kb_id)
    return f is None or on(cfg, f)


# ---- probes ---------------------------------------------------------------------------

def probe_fn(f: Feature):
    """The probe beside the module, resolved when asked (so a test may replace it)."""
    if not f.probe:
        return None
    module, _, name = f.probe.partition(":")
    return getattr(importlib.import_module(module), name)


async def probe(cfg, services, feature: str) -> dict[str, str]:
    """One feature's state now: {"state", "detail"}, with "url" when the probe found the
    service at its default address while none is saved (the setup form prefills it).
    "none" when it has nothing to detect; a probe that does not answer within
    PROBE_SECONDS, or raises, is "missing"."""
    fn = probe_fn(BY_ID[feature])
    if fn is None:
        return {"state": "none", "detail": ""}
    try:
        with anyio.fail_after(PROBE_SECONDS):
            result = await fn(cfg, services)
    except TimeoutError:
        return {"state": "missing", "detail": f"{BY_ID[feature].label} did not answer in time."}
    except Exception as e:                       # a probe never takes the page down
        log.warning("feature probe failed", extra={"event": "features.probe_failed",
                                                   "feature": feature,
                                                   "error": type(e).__name__})
        return {"state": "missing",
                "detail": f"{BY_ID[feature].label} could not be checked ({type(e).__name__})."}
    if result is None:
        return {"state": "none", "detail": ""}
    out = {"state": result["state"], "detail": result.get("detail", "")}
    if result.get("url"):
        out["url"] = result["url"]
    return out


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
        for f in CATALOG:
            tg.start_soon(one, f)
    out = {f.id: out[f.id] for f in CATALOG}
    if services is not None:
        services.feature_probes = (time.monotonic(), out)
    return out
