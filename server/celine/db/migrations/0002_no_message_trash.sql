-- No message trash: a deleted message is gone at once. What was trashed goes now, with
-- the results of its tool calls; what followed it moves under its nearest kept ancestor,
-- as the old "delete permanently" did.
CREATE TEMP TABLE gone (id TEXT PRIMARY KEY);
INSERT INTO gone SELECT id FROM messages WHERE trashed_at IS NOT NULL;
INSERT OR IGNORE INTO gone
  SELECT result_message_id FROM tool_calls
  WHERE message_id IN (SELECT id FROM gone) AND result_message_id IS NOT NULL;

CREATE TEMP TABLE kept_parent AS
  WITH RECURSIVE up(start, id, pid) AS (
    SELECT m.id, p.id, p.parent_id FROM messages m JOIN messages p ON p.id = m.parent_id
    WHERE m.id NOT IN (SELECT id FROM gone) AND m.parent_id IN (SELECT id FROM gone)
    UNION ALL
    SELECT up.start, q.id, q.parent_id FROM up JOIN messages q ON q.id = up.pid
    WHERE up.id IN (SELECT id FROM gone))
  SELECT m.id AS id,
         (SELECT u.id FROM up u WHERE u.start = m.id AND u.id NOT IN (SELECT id FROM gone))
           AS parent
  FROM messages m
  WHERE m.id NOT IN (SELECT id FROM gone) AND m.parent_id IN (SELECT id FROM gone);
UPDATE messages SET parent_id = (SELECT parent FROM kept_parent k WHERE k.id = messages.id)
  WHERE id IN (SELECT id FROM kept_parent);

UPDATE conversations SET active_leaf_id = (
    WITH RECURSIVE up(id, pid) AS (
      SELECT id, parent_id FROM messages WHERE id = conversations.active_leaf_id
      UNION ALL
      SELECT q.id, q.parent_id FROM up JOIN messages q ON q.id = up.pid
      WHERE up.id IN (SELECT id FROM gone))
    SELECT id FROM up WHERE id NOT IN (SELECT id FROM gone))
  WHERE active_leaf_id IN (SELECT id FROM gone);

UPDATE messages SET preferred_child_id = NULL WHERE preferred_child_id IN (SELECT id FROM gone);
UPDATE moods SET message_id = NULL WHERE message_id IN (SELECT id FROM gone);
UPDATE turns SET root_message_id = NULL WHERE root_message_id IN (SELECT id FROM gone);
UPDATE tool_calls SET result_message_id = NULL WHERE result_message_id IN (SELECT id FROM gone);
UPDATE intercom_deliveries SET message_id = NULL WHERE message_id IN (SELECT id FROM gone);
DELETE FROM message_context WHERE message_id IN (SELECT id FROM gone);
DELETE FROM compactions WHERE through_message_id IN (SELECT id FROM gone);

CREATE TEMP TABLE gone_calls AS
  SELECT id FROM tool_calls WHERE message_id IN (SELECT id FROM gone);
UPDATE jobs SET tool_call_id = NULL WHERE tool_call_id IN (SELECT id FROM gone_calls);
DELETE FROM attachments WHERE message_id IN (SELECT id FROM gone)
  OR tool_call_id IN (SELECT id FROM gone_calls)
  OR batch_id IN (SELECT id FROM media_batches
                  WHERE tool_call_id IN (SELECT id FROM gone_calls));
DELETE FROM media_batches WHERE tool_call_id IN (SELECT id FROM gone_calls);
DELETE FROM tool_calls WHERE id IN (SELECT id FROM gone_calls);
DELETE FROM kv WHERE EXISTS (SELECT 1 FROM gone g WHERE instr(kv.k, g.id) > 0);
DELETE FROM messages WHERE id IN (SELECT id FROM gone);

DROP TABLE gone_calls;
DROP TABLE kept_parent;
DROP TABLE gone;
ALTER TABLE messages DROP COLUMN trashed_at;
