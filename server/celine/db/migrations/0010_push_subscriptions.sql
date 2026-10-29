-- Web push (push.py): one row per device that takes pushes, with the notice kinds it takes
-- and the login session it ends with (auth.db sessions.id; NULL with the login off).
CREATE TABLE push_subscriptions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  endpoint    TEXT NOT NULL UNIQUE,
  p256dh      TEXT NOT NULL,
  auth        TEXT NOT NULL,
  device      TEXT NOT NULL,
  kinds       TEXT NOT NULL,
  session_id  INTEGER,
  created_at  TEXT NOT NULL
);
