"""Profiles read slowly, one at a time: the first pass, then the oldest again. A deleted
account keeps no names in its record. Fake Discord only."""
from __future__ import annotations

from discord_link import profiles, records
from discord_link.state import now_ms


def people(state):
    return [r["person"] for r in state.after(0, 10000) if r["kind"] == "person"]


def test_one_profile_at_a_time_far_apart(link, discord, state, monkeypatch):
    discord.profiles = {
        "U2": {"user": {"id": "U2", "username": "shiniloli", "global_name": "Moon"},
               "user_profile": {"bio": "sleepy", "pronouns": "she/her"}},
        "U3": "gone", "U4": None}
    for uid in ("U2", "U3", "U4"):
        state.journal([{"kind": "person", "person": {"key": uid, "username": uid.lower()}}])
    state.journal([{"kind": "person", "person": {"key": "ME", "is_me": True}}])
    assert profiles.step(link.rest, state) == "U2"
    assert profiles.step(link.rest, state) is None, "the next one waits FIRST_SECONDS"
    assert people(state)[-1] == {"key": "U2", "username": "shiniloli", "display_name": "Moon",
                                 "bio": "sleepy", "pronouns": "she/her", "deleted": False,
                                 "profile": True}
    t = now_ms()
    monkeypatch.setattr(profiles, "now_ms", lambda: t + 121_000)
    assert profiles.step(link.rest, state) == "U3"            # 404: the account is gone
    assert people(state)[-1] == {"key": "U3", "deleted": True}
    monkeypatch.setattr(profiles, "now_ms", lambda: t + 242_000)
    before = len(people(state))
    assert profiles.step(link.rest, state) == "U4"            # 403: not shown, nothing said
    assert len(people(state)) == before
    # everyone read once: the next read is the oldest, REFRESH_SECONDS later
    monkeypatch.setattr(profiles, "now_ms", lambda: t + 363_000)
    assert profiles.due(state) is None
    monkeypatch.setattr(profiles, "now_ms", lambda: t + 242_000 + 901_000)
    assert profiles.due(state) == "U2"
    asked = [p for _, p, _ in discord.requests if p.endswith("/profile")]
    assert asked == ["/users/U2/profile", "/users/U3/profile", "/users/U4/profile"]


def test_a_deleted_account_and_a_server_nickname_in_a_record():
    assert records.person({"id": "9", "username": "deleted_user_ab12", "global_name": None}
                          ) == {"key": "9", "deleted": True}
    assert records.person({"id": "9", "username": "moon"}, nick="Moony",
                          nick_scope="Chess") == {"key": "9", "username": "moon",
                                                  "nick": "Moony", "nick_scope": "Chess"}
    assert "nick_scope" not in records.person({"id": "9", "username": "moon"},
                                              nick_scope="Chess")
