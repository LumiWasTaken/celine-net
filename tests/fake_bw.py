"""A fake Bitwarden CLI, `bw` on the tests' PATH (vault_fixtures.py): it answers logout,
config server, login --apikey, unlock, lock, sync, list items --collectionid, list
collections, get item, create item and edit item from the JSON file FAKE_BW, as the real
one answers, and appends every command line it was given to FAKE_BW_LOG. An edit that
changes the password keeps the old one first in the item's passwordHistory, as Bitwarden
does. "odd" in the state: a create fails with that text, one the app has no sentence for."""
import base64
import json
import os
import sys
import uuid

args = sys.argv[1:]
path = os.environ["FAKE_BW"]
with open(path) as f:
    st = json.load(f)
with open(os.environ["FAKE_BW_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")


def fail(message: str):
    print(message, file=sys.stderr)
    sys.exit(1)


def online():
    if not st["reachable"]:
        fail(f"FetchError: request to {st['url']}/identity/connect/token failed, reason: "
             f"connect ECONNREFUSED 127.0.0.1:443")


def unlocked():
    if not st["logged_in"]:
        fail("You are not logged in.")
    if os.environ.get("BW_SESSION") != st["session"]:
        fail("Vault is locked.")


def read_item() -> dict:
    return json.loads(base64.b64decode(sys.stdin.read()))


if args == ["logout"]:
    if not st["logged_in"]:
        fail("You are not logged in.")
    st["logged_in"] = False
    print("You have logged out.")
elif args == ["lock"]:
    st["session"] = st["session"] + "-next"     # the old key opens nothing any more
    print("Your vault is locked.")
elif args[:2] == ["config", "server"]:
    if st["logged_in"]:
        fail("Logout required before server config update.")
    st["url"] = args[2]
    print("Saved setting `config`.")
elif args == ["login", "--apikey"]:
    online()
    if (os.environ.get("BW_CLIENTID"), os.environ.get("BW_CLIENTSECRET")) != \
            (st["client_id"], st["client_secret"]):
        fail("client_id or client_secret is incorrect. Try again.")
    st["logged_in"] = True
    print("You are logged in!")
elif args[:1] == ["unlock"]:
    if not st["logged_in"]:
        fail("You are not logged in.")
    if os.environ.get(args[args.index("--passwordenv") + 1]) != st["master"]:
        fail("Invalid master password.")
    print(st["session"], end="")
elif args == ["sync"]:
    unlocked()
    online()
    st["syncs"] += 1
    print("Syncing complete.")
elif args[:3] == ["list", "items", "--collectionid"]:
    unlocked()
    print(json.dumps([i for i in st["items"] if args[3] in (i.get("collectionIds") or [])]))
elif args == ["list", "collections"]:
    unlocked()
    print(json.dumps(st.get("collections", [])))
elif args[:2] == ["get", "item"]:
    unlocked()
    it = next((i for i in st["items"] if args[2] in (i["id"], i["name"])), None)
    if it is None:
        fail("Not found.")
    print(json.dumps(it))
elif args == ["create", "item"]:
    unlocked()
    online()
    if st.get("odd"):
        fail(st["odd"])
    it = {**read_item(), "id": str(uuid.uuid4()), "passwordHistory": None}
    if it.get("organizationId") and not it.get("collectionIds"):
        fail("Organization items need at least one collection.")
    st["items"].append(it)
    print(json.dumps(it))
elif args[:2] == ["edit", "item"]:
    unlocked()
    online()
    new = read_item()
    k = next(k for k, it in enumerate(st["items"]) if it["id"] == args[2])
    old = st["items"][k]["login"]["password"]
    if st["history"] and old != new["login"]["password"]:
        new["passwordHistory"] = [{"lastUsedDate": "2026-10-11T08:00:00.000Z",
                                   "password": old}, *(new.get("passwordHistory") or [])]
    st["items"][k] = new
    print(json.dumps(new))
else:
    fail(f"Invalid command: {' '.join(args)}")

with open(path, "w") as f:
    json.dump(st, f)
