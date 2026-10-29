-- Per step (turns/runner.py _record_usage): the seconds to its first token, in ms, and
-- the prompt tokens the server read from its prompt cache, where it says so.
ALTER TABLE requests ADD COLUMN first_token_ms INTEGER;
ALTER TABLE requests ADD COLUMN cache_read INTEGER;
