-- A sub-agent's call names where its conversation opens (homes.of), so the client never
-- works the link out; its display carries `link` from now on (sub_agents/skill.py), and
-- the calls made before now get it here. The home follows the root, the top of the
-- agent_of chain: a mission's opens under Missions, a background one's under Routines
-- (the night of its run, else of its root's start), any other with her; a bridged chat's
-- (0005 makes it a chat) with her too. A conversation
-- that is gone has none.
UPDATE tool_calls SET display = json_set(display, '$.link', (
  WITH RECURSIVE up(id, agent_of, depth) AS (
    SELECT id, agent_of, 0 FROM conversations
     WHERE id = json_extract(tool_calls.display, '$.conversation_id')
    UNION ALL
    SELECT c.id, c.agent_of, up.depth + 1 FROM conversations c JOIN up ON c.id = up.agent_of
     WHERE up.depth < 32
  ),
  top AS (SELECT c.* FROM up JOIN conversations c ON c.id = up.id ORDER BY up.depth DESC LIMIT 1),
  sub AS (SELECT id FROM up WHERE depth = 0)
  SELECT CASE
    WHEN top.kind = 'mission' THEN '/mission/' || sub.id
    WHEN top.kind = 'background' AND coalesce(top.trigger_key, '') NOT LIKE 'bridge:%' THEN '/routines/' || coalesce(
      (SELECT r.night FROM runs r
        WHERE top.trigger_key LIKE 'run:%'
          AND r.id = CASE WHEN instr(substr(top.trigger_key, 5), ':') > 0
                          THEN substr(top.trigger_key, 5, instr(substr(top.trigger_key, 5), ':') - 1)
                          ELSE substr(top.trigger_key, 5) END),
      substr(top.created_at, 1, 10)) || '/task/' || sub.id
    ELSE '/chat/' || sub.id END
  FROM top, sub))
WHERE name LIKE 'sub\_agents\_\_%' ESCAPE '\'
  AND json_valid(display) AND json_type(display, '$.conversation_id') = 'text';
