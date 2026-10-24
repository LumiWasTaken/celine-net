-- A message deleted on the source keeps only a tombstone (messages.deleted_at, its time
-- and sender): its text, file list, earlier texts and file details go.
DELETE FROM message_edits WHERE message_id IN
    (SELECT id FROM messages WHERE deleted_at IS NOT NULL);
DELETE FROM media_fetch WHERE message_id IN
    (SELECT id FROM messages WHERE deleted_at IS NOT NULL);
DELETE FROM wa_media_meta WHERE key IN
    (SELECT key FROM messages WHERE deleted_at IS NOT NULL);
UPDATE messages SET text = '', media = NULL, edited = 0 WHERE deleted_at IS NOT NULL;
