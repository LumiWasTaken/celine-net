-- The bridged chat (WhatsApp) is gone: its conversations are chats like any other, and
-- the marks only it read go: where an intercom message came from or went (`via`, the
-- chat's keys `sent`, `urgent`) and an image batch item that also went there (`bridged`).
UPDATE conversations SET kind = 'chat', trigger_key = NULL
  WHERE trigger_key LIKE 'bridge:%';
UPDATE intercom SET data = json_remove(data, '$.via', '$.sent', '$.urgent')
  WHERE json_type(data, '$.via') IS NOT NULL OR json_type(data, '$.urgent') IS NOT NULL;
UPDATE media_batches SET items = (
    SELECT json_group_array(json(json_remove(value, '$.bridged')))
    FROM (SELECT value FROM json_each(media_batches.items) ORDER BY key))
  WHERE EXISTS (SELECT 1 FROM json_each(media_batches.items)
                WHERE json_type(value, '$.bridged') IS NOT NULL);
