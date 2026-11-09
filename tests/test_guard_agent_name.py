"""{{agent_name}} in the judges' lines: a sub-agent's own name in its turns, the agent's in the agent's."""
import sqlite3

from celine.guard.intent import agent_name


def test_agent_name_is_the_sub_agents_own_name_and_the_agents_elsewhere():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE conversations (id TEXT, agent_name TEXT)")
    c.execute("CREATE TABLE agent (id INTEGER, name TEXT)")
    c.executemany("INSERT INTO conversations VALUES (?, ?)", [("a", "finder"), ("b", None)])
    c.execute("INSERT INTO agent VALUES (1, 'Celine')")
    line = "{{agent_name}} said: on it"
    assert agent_name(c, {"conversation_id": "a"}, line) == "finder said: on it"
    assert agent_name(c, {"conversation_id": "b"}, line) == "Celine said: on it"
