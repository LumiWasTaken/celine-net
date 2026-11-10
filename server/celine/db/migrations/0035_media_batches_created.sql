-- the Gallery lists every kind of batch newest first
CREATE INDEX media_batches_created ON media_batches(created_at, id);
