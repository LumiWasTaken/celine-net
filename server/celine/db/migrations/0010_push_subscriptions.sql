-- Web push (push.py): one row per device that takes pushes, with the notice kinds it takes.
CREATE TABLE push_subscriptions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  endpoint    TEXT NOT NULL UNIQUE,
  p256dh      TEXT NOT NULL,
  auth        TEXT NOT NULL,
  device      TEXT NOT NULL,
  kinds       TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
