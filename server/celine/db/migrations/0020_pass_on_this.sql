-- "Pass on this" on Bastet's banner (guard/passes.py): a pass that covers only the calls
-- whose evidence for its rule is this exact piece; NULL for a pass on the whole rule.
ALTER TABLE guard_passes ADD COLUMN piece TEXT;
