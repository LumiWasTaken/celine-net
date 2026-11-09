-- A mission's last check-in (missions/watch.py check_ins): to the user it is a notice,
-- no intercom message, so the time is kept on the mission; the ones sent before as
-- intercom messages carry theirs over.
ALTER TABLE missions ADD COLUMN checked_in_at TEXT;
UPDATE missions SET checked_in_at = (
  SELECT max(created_at) FROM intercom
  WHERE from_addr = 'agent:' || missions.conversation_id AND json_extract(data, '$.check_in'));
-- A check-in told, it never asked: the ones still unread stop waiting for the user, so
-- Waiting for you does not show them as the mission's question (waits.py _missions).
UPDATE intercom_deliveries SET state = 'read',
  delivered_at = coalesce(delivered_at, strftime('%Y-%m-%dT%H:%M:%f', 'now') || '+00:00')
WHERE to_addr = 'user' AND state = 'pending' AND intercom_id IN (
  SELECT id FROM intercom WHERE from_addr LIKE 'agent:%' AND json_extract(data, '$.check_in'));
