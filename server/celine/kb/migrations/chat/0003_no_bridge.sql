-- The bridged chat is gone: its cursor, its switch and the marker the user set go.
DELETE FROM kv WHERE key IN ('bridge_cursor', 'bridge_since', 'marker_own');
