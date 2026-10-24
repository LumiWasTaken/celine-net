-- A session gets an id (the Security list ends one by it, never by its token hash), the
-- browser's user agent, trimmed, and when it last proved it is the owner (its login or
-- /api/auth/reauth): adding a way to log in, new recovery codes and a named token need a
-- proof from the last few minutes. A session from before has none yet.
CREATE TABLE sessions_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash    BLOB NOT NULL UNIQUE,
    passkey_id    BLOB REFERENCES passkeys(id) ON DELETE CASCADE,
    credential_id INTEGER REFERENCES credentials(id) ON DELETE CASCADE,
    user_agent    TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    refreshed_at  INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    proved_at     INTEGER NOT NULL,
    CHECK ((passkey_id IS NULL) != (credential_id IS NULL))
);
INSERT INTO sessions_new (token_hash, passkey_id, credential_id, created_at, refreshed_at,
                          expires_at, proved_at)
    SELECT token_hash, passkey_id, credential_id, created_at, refreshed_at, expires_at, 0
    FROM sessions ORDER BY created_at;
DROP TABLE sessions;
ALTER TABLE sessions_new RENAME TO sessions;
