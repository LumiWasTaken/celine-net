"""discord-link: the Discord connector of celine-net.

One process owns the Discord account (the user token from a QR login), reads with
REST and the gateway, and gives the app three doors on
one port, each behind a bearer key: `/mcp` (Celine's read tools), `/feed` (the journal
as NDJSON for the archive) and `/admin` (login, lists, subscriptions, sync).
"""
