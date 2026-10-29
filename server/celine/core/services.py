"""Everything the running app holds, handed around as one object.

build_app makes it and the lifespan fills each field as its object starts. Skills get it
from `skills.build` as `self.app` and read a field when a tool runs, never when they are
built, so a skill rebuilt by a config reload needs no rewiring. The routes have the same
object as `app.state.services`. A test fills only what it uses.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Services:
    cfg: Any = None
    store: Any = None
    bus: Any = None
    llm: Any = None
    kbs: Any = None
    registry: Any = None
    dispatcher: Any = None
    intercom: Any = None
    terminals: Any = None
    mcp: Any = None
    connectors: Any = None
    batches: Any = None
    triggers: Any = None
    schedules: Any = None
    vault: Any = None
    approvals: Any = None
    push: Any = None                # push.py: web push to the user's devices
    runners: Any = None             # core/runners.py: what runs for each service feature
    feature_probes: Any = None      # settings/features.py probe_all(): (monotonic, {id: probe})
