-- A turn's end hooks (the dispatcher's on_end) run once; this marks that they did, so a
-- restart can run the ones a shutdown cut off. Every turn before now is marked.
ALTER TABLE turns ADD COLUMN ended_hooks INTEGER NOT NULL DEFAULT 0;
UPDATE turns SET ended_hooks = 1;
