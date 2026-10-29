-- A mission's minimum time (missions/skill.py update), the seconds the house held it
-- (missions/watch.py credit: its times move later by them), and the conversation she
-- started it from (missions__start; NULL: the user started it).
ALTER TABLE missions ADD COLUMN min_until TEXT;
ALTER TABLE missions ADD COLUMN held_seconds INTEGER NOT NULL DEFAULT 0;
ALTER TABLE missions ADD COLUMN started_by TEXT;
