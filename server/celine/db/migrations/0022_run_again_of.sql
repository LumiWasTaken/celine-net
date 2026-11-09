-- A Retry run (schedules/runs.py _fail and POST /api/runs/{id}/retry): the run it runs
-- again for the same night; NULL for a run of its own slot.
ALTER TABLE runs ADD COLUMN again_of TEXT REFERENCES runs(id) ON DELETE SET NULL;
