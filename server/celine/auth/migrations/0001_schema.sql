-- The login (server/celine/auth), apart from celine.db. Every hash is scrypt: 16 bytes
-- of salt, then the 32-byte digest (credentials.py).

-- the one user the passkeys belong to: a random WebAuthn user handle
CREATE TABLE owner (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    user_handle BLOB NOT NULL
);
INSERT INTO owner (id, user_handle) VALUES (1, randomblob(16));

CREATE TABLE passkeys (
    id           BLOB PRIMARY KEY,          -- the WebAuthn credential id
    public_key   BLOB NOT NULL,             -- COSE public key
    sign_count   INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER
);

-- a password or a PIN
CREATE TABLE credentials (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('password', 'pin')),
    hash       BLOB NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

-- only the SHA-256 of a session token is stored; a session belongs to the passkey or
-- the credential it was made with
CREATE TABLE sessions (
    token_hash    BLOB PRIMARY KEY,
    passkey_id    BLOB REFERENCES passkeys(id) ON DELETE CASCADE,
    credential_id INTEGER REFERENCES credentials(id) ON DELETE CASCADE,
    created_at    INTEGER NOT NULL,
    refreshed_at  INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    CHECK ((passkey_id IS NULL) != (credential_id IS NULL))
);

-- single use
CREATE TABLE recovery_codes (
    id      INTEGER PRIMARY KEY,
    hash    BLOB NOT NULL,
    used_at INTEGER
);

-- the one-time setup code, present only while nothing can log in
CREATE TABLE setup_code (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    hash       BLOB NOT NULL,
    created_at INTEGER NOT NULL
);

-- wrong tries: one lock for setup and recovery codes, one for passwords and PINs
CREATE TABLE locks (
    kind         TEXT PRIMARY KEY CHECK (kind IN ('code', 'secret')),
    failures     INTEGER NOT NULL,
    locked_until INTEGER NOT NULL
);
