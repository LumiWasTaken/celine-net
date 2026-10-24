-- the "secret" lock counts per client address; "code" stays one lock for everyone
-- (addr ''). A global secret lock from before goes: it was the one anyone could trip.
CREATE TABLE locks_new (
    kind         TEXT NOT NULL CHECK (kind IN ('code', 'secret')),
    addr         TEXT NOT NULL,
    failures     INTEGER NOT NULL,
    locked_until INTEGER NOT NULL,
    PRIMARY KEY (kind, addr)
);
INSERT INTO locks_new (kind, addr, failures, locked_until)
    SELECT kind, '', failures, locked_until FROM locks WHERE kind = 'code';
DROP TABLE locks;
ALTER TABLE locks_new RENAME TO locks;
