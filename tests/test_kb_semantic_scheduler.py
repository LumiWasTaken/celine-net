"""The semantic refresh looks at the archives every smart_search_refresh_minutes (Settings,
General) and after a settings
change, not on every wake of the router: a build that finds no embedding model fails
it, which wakes the loop, which would build again without pause and keep the archive's
writer busy."""
import asyncio
from types import SimpleNamespace

import pytest

from celine.backends import router as router_mod
from celine.kb import semantic


class Stop(Exception):
    pass


def run(wakes: list, cfg, monkeypatch) -> list[str]:
    """Run the scheduler through `wakes` (each a callable run on that wake); the starts."""
    started: list[str] = []
    kb = SimpleNamespace(id="whatsapp", file=SimpleNamespace(read=None))
    tasks = SimpleNamespace(
        running=lambda kid, kind: False,
        job=lambda kid, kind: {"state": "unreachable", "finished_at": 0},
        start=lambda kid, kind, label, fn, quiet: started.append(kid))
    kbs = SimpleNamespace(chats=lambda: [kb], tasks=tasks)
    todo = list(wakes)
    clock = [1000.0]

    class Router:
        async def wait_change(self, timeout):
            if not todo:
                raise Stop
            todo.pop(0)(clock)

    monkeypatch.setattr(router_mod, "app_router", lambda cfg: Router())
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    with pytest.raises(Stop):
        asyncio.run(semantic.run_scheduler(cfg, kbs))
    return started


def cfg():
    return SimpleNamespace(settings=SimpleNamespace(general={"smart_search_refresh_minutes": 15}))


def nothing(clock):
    pass


def test_it_looks_at_start_after_a_settings_change_and_on_its_interval_only(monkeypatch):
    # the first look builds; its own failure wakes the loop five times: no rebuild
    assert run([nothing] * 5, cfg(), monkeypatch) == ["whatsapp"]
    c = cfg()
    wakes = [nothing, lambda clock: setattr(c, "settings", SimpleNamespace(general={})), nothing]
    assert run(wakes, c, monkeypatch) == ["whatsapp", "whatsapp"]

    def later(clock):
        clock[0] += 15 * 60
    assert run([nothing, later, nothing], cfg(), monkeypatch) == ["whatsapp", "whatsapp"]


def run_once(kbs_list, cfg, monkeypatch) -> list[str]:
    """One scheduler look over real knowledge bases (no job state forcing a build):
    return the ids it started."""
    started: list[str] = []
    tasks = SimpleNamespace(
        running=lambda kid, kind: False,
        job=lambda kid, kind: None,
        start=lambda kid, kind, label, fn, quiet: started.append(kid))
    kbs = SimpleNamespace(chats=lambda: list(kbs_list), tasks=tasks)
    clock = [1000.0]

    class Router:
        async def wait_change(self, timeout):
            raise Stop

    monkeypatch.setattr(router_mod, "app_router", lambda cfg: Router())
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    with pytest.raises(Stop):
        asyncio.run(semantic.run_scheduler(cfg, kbs))
    return started


def test_a_cold_archive_is_started_and_an_empty_one_is_left_alone(tmp_path, monkeypatch):
    from celine.kb.chat import ChatKB
    from kb_fixtures import archive

    cold = ChatKB(id="cold", title="Cold", platform="whatsapp", path=tmp_path / "cold.db")
    archive(cold)                       # imported messages, no passages yet
    empty = ChatKB(id="empty", title="Empty", platform="whatsapp",
                   path=tmp_path / "empty.db")
    assert run_once([cold, empty], cfg(), monkeypatch) == ["cold"]
