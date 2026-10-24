"""The email skill against a fake IMAP server in a thread: list, search and attachments
never change a flag, reading marks read, it marks, moves and deletes, and the settings
routes store the password write-only."""
from __future__ import annotations

import email
import email.policy
import re
import socketserver
import threading
from email.message import EmailMessage

import imaplib
import pytest

from celine import boot
from celine.skills import ToolContext, mail
from settings_fixtures import client, make_app, scratch

pytestmark = pytest.mark.anyio

PASSWORD = "pw-5b1f0c7a9e2d4d61"


def _msg(sender, subject, date, text, *, html=None, attach=None) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = sender, "celine@example.com", subject, date
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
    if attach:
        m.add_attachment(attach[1], filename=attach[0])
    return m.as_bytes()


def _html_only() -> bytes:
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"] = "news@shop.de", "Angebote", "Sun, 04 Oct 2026 08:00:00 +0000"
    m.set_content("<html><body><h1>Herbst</h1><p>Alles über Pilze.</p>"
                  "<script>x()</script></body></html>", subtype="html")
    return m.as_bytes()


def mailbox():
    return {
        "INBOX": [
            [3, set(), _msg("Anna <anna@example.com>", "Fotos vom Wochenende",
                            "Fri, 02 Oct 2026 18:30:00 +0200", "Hier die Liste.",
                            attach=("liste.txt", "Pilze\nKastanien\n"))],
            [7, {"\\Seen"}, _html_only()],
            [9, set(), _msg("Ben <ben@example.com>", "Rechnung", "Wed, 07 Oct 2026 09:00:00 "
                            "+0200", "Die Rechnung kommt morgen.")],
        ],
        "Entwürfe": [],
        "Papierkorb": [],                    # \Trash
        "[Gmail]": None,                     # \Noselect
    }


class FakeIMAP(socketserver.StreamRequestHandler):
    """The handful of IMAP4rev1 commands imaplib sends for the skill."""
    box: dict = {}
    log: list = []
    caps = "IMAP4rev1 AUTH=PLAIN MOVE UIDPLUS"
    disable_nagle_algorithm = True       # many small writes: no 40 ms delayed-ack stalls

    def out(self, line: bytes | str) -> None:
        self.wfile.write((line.encode() if isinstance(line, str) else line) + b"\r\n")

    def handle(self):
        self.out("* OK fake ready")
        current = None
        while line := self.rfile.readline():
            line = line.rstrip(b"\r\n")
            literal = re.search(rb"\{(\d+)\}$", line)
            if literal:                       # a search value sent as a literal
                self.out("+ go")
                value = self.rfile.read(int(literal.group(1)))
                self.rfile.readline()
                line = line[:literal.start()] + b'"' + value + b'"'
            tag, cmd, *rest = line.decode().split(" ", 2)
            arg = rest[0] if rest else ""
            self.log.append(f"{cmd} {arg}".strip())
            cmd = cmd.upper()
            if cmd == "CAPABILITY":
                self.out(f"* CAPABILITY {self.caps}")
            elif cmd == "LOGIN":
                if arg != f'carla@example.com "{PASSWORD}"':
                    self.out(f"{tag} NO [AUTHENTICATIONFAILED] wrong")
                    continue
            elif cmd == "LOGOUT":
                self.out("* BYE")
                self.out(f"{tag} OK bye")
                return
            elif cmd == "LIST":
                for name, msgs in self.box.items():
                    flags = "\\Noselect" if msgs is None else "\\HasNoChildren" + (
                        " \\Trash" if name == "Papierkorb" else "")
                    self.out(f'* LIST ({flags}) "/" "{mail._encode_folder(name)}"')
            elif cmd == "STATUS":
                name = mail._decode_folder(arg.split('"')[1])
                msgs = self.box[name]
                unseen = sum(1 for m in msgs if "\\Seen" not in m[1])
                self.out(f'* STATUS "{arg.split(chr(34))[1]}" (MESSAGES {len(msgs)} '
                         f'UNSEEN {unseen})')
            elif cmd == "CREATE":
                self.box[mail._decode_folder(arg.strip('"'))] = []
            elif cmd == "EXPUNGE":
                current[:] = [m for m in current if "\\Deleted" not in m[1]]
            elif cmd in ("EXAMINE", "SELECT"):
                current = self.box.get(mail._decode_folder(arg.strip('"')))
                if current is None:
                    self.out(f"{tag} NO no such folder")
                    continue
                self.out(f"* {len(current)} EXISTS")
            elif cmd == "UID":
                sub, _, args = arg.partition(" ")
                if sub.upper() == "SEARCH":
                    hits = [m for m in current if self.matches(m, args)]
                    self.out("* SEARCH " + " ".join(str(m[0]) for m in hits))
                elif sub.upper() == "FETCH":
                    uids, _, what = args.partition(" ")
                    want = {int(u) for u in uids.split(",")}
                    for seq, m in enumerate(current, 1):
                        if m[0] not in want:
                            continue
                        raw = m[2] if "[]" in what else m[2].split(b"\n\n")[0] + b"\n\n"
                        flags = " ".join(sorted(m[1]))
                        self.out(f"* {seq} FETCH (UID {m[0]} RFC822.SIZE {len(m[2])} "
                                 f"BODY[] {{{len(raw)}}}".encode())
                        self.wfile.write(raw)
                        self.out(f" FLAGS ({flags}))")
                else:
                    uids, _, rest = args.partition(" ")
                    want = {int(u) for u in uids.split(",")}
                    hit = [m for m in current if m[0] in want]
                    if sub.upper() == "STORE":
                        how, _, flags = rest.partition(" ")
                        for m in hit:
                            for f in flags.strip("()").split():
                                (m[1].add if how.startswith("+") else m[1].discard)(f)
                    elif sub.upper() in ("MOVE", "COPY"):
                        dest = self.box[mail._decode_folder(rest.strip('"'))]
                        for m in hit:
                            top = max((d[0] for d in dest), default=0)
                            dest.append([top + 1, set(m[1]), m[2]])
                        if sub.upper() == "MOVE":
                            current[:] = [m for m in current if m[0] not in want]
                    elif sub.upper() == "EXPUNGE":
                        current[:] = [m for m in current
                                      if not (m[0] in want and "\\Deleted" in m[1])]
            self.out(f"{tag} OK done")

    @staticmethod
    def matches(m, args: str) -> bool:
        msg = email.message_from_bytes(m[2], policy=email.policy.default)
        text = " ".join([str(msg["from"]), str(msg["subject"])] + [
            p.get_content() for p in msg.walk() if p.get_content_maintype() == "text"]).lower()
        if hit := re.match(r"UID ([\d,]+)", args):
            return m[0] in {int(u) for u in hit.group(1).split(",")}
        for key, value in re.findall(r'(TEXT|FROM) "([^"]*)"', args):
            if value.lower() not in text:
                return False
        if "UNSEEN" in args and "\\Seen" in m[1]:
            return False
        return True


@pytest.fixture
def imap(monkeypatch):
    FakeIMAP.box, FakeIMAP.log = mailbox(), []
    FakeIMAP.caps = "IMAP4rev1 AUTH=PLAIN MOVE UIDPLUS"
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeIMAP)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    # the fake speaks plain IMAP; the app's TLS is the stdlib's own
    monkeypatch.setattr(mail, "_open", lambda a, t: imaplib.IMAP4(a.host, a.port, timeout=t))
    mail._unread.clear()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
async def api(tmp_path, monkeypatch, store, imap):
    scratch(tmp_path, monkeypatch)
    cfg = boot.load()
    app = make_app(cfg, store)
    async with client(app) as c:
        c.cfg, c.port, c.app = cfg, imap, app
        yield c


async def _setup(api, password=PASSWORD):
    r = await api.put("/api/email", json={
        "email": {"host": "127.0.0.1", "port": api.port, "tls": "starttls",
                  "user": "carla@example.com"}, "password": password})
    assert r.status_code == 200, r.text
    return r


async def test_settings_store_the_password_write_only_and_test_logs_in(api):
    assert (await api.post("/api/email/test")).json()["ok"] is False
    r = await _setup(api)
    assert PASSWORD not in r.text and r.json()["password"]["state"] == "set"
    got = await api.get("/api/email")
    assert PASSWORD not in got.text and got.json()["email"]["port"] == api.port
    t = (await api.post("/api/email/test")).json()
    assert t["ok"] is True, t
    assert {f["name"]: (f["messages"], f["unread"]) for f in t["folders"]} == \
        {"INBOX": (3, 2), "Entwürfe": (0, 0), "Papierkorb": (0, 0)}
    # a PUT without the password keeps it; a wrong one is one sentence
    await api.put("/api/email", json={"email": {
        "host": "127.0.0.1", "port": api.port, "tls": "starttls", "user": "carla@example.com"}})
    assert (await api.post("/api/email/test")).json()["ok"] is True
    await _setup(api, "wrong")
    t = (await api.post("/api/email/test")).json()
    assert t == {"ok": False, "folders": [], "sentence":
                 "The mail server at 127.0.0.1 refused the login for carla@example.com: check the password in Settings, Email."}
    bad = await api.put("/api/email", json={"email": {"tls": "none"}})
    assert bad.status_code == 400 and bad.json()["error"]["detail"]["field"] == "email.tls"


async def _ctx(api):
    from chat_fixtures import user_says
    from celine.conversations import store as conversations, tree
    from celine.turns import store as turns
    await _setup(api)
    skill = api.app.state.services.registry.skills["email"]
    cid = await conversations.create(api.app.state.services.store)
    _, tid, _ = await user_says(api.app.state.services.store, cid, "my mail?")
    mid = await tree.start_assistant_message(api.app.state.services.store, cid, None, tid)
    await turns.record_tool_calls(api.app.state.services.store, cid, mid, [
        {"id": "c1", "name": "email__get_attachment", "arguments": "{}"}])
    return skill, ToolContext(conversation_id=cid, turn_id=tid, tool_call_id="c1",
                              store=api.app.state.services.store, config=api.cfg, emit=None)  # type: ignore[arg-type]


async def test_list_search_and_attachment_leave_every_flag_reading_marks_read(api):
    skill, ctx = await _ctx(api)
    assert skill.status == "up" and "email__read" in api.app.state.services.registry.tools

    assert await skill.prompt_fragment(ctx) == "Email: carla@example.com on 127.0.0.1, 2 unread."
    listed = (await skill.list(ctx, limit=2)).content
    assert listed.splitlines()[1].startswith("[INBOX:9] 2026-10-07 09:00  from Ben")
    assert "attachments" not in listed.splitlines()[1]
    assert 'more: email__list(folder="INBOX", before="INBOX:7")' in listed
    older = (await skill.list(ctx, before="INBOX:7")).content
    assert "[INBOX:3]" in older and "attachments, unread" in older
    assert "INBOX:7" not in (await skill.list(ctx, unread_only=True)).content

    found = (await skill.search(ctx, from_="anna")).content
    assert "1 match" in found and "[INBOX:3]" in found
    assert "[INBOX:7]" in (await skill.search(ctx, query="über Pilze", since="2026-10-01")).content
    assert (await skill.search(ctx, since="gestern")).is_error

    read = (await skill.read(ctx, "INBOX:3")).content
    assert "From: Anna <anna@example.com>" in read and "Hier die Liste." in read
    assert "Attachments: liste.txt (text/plain" in read and "(was unread)" in read
    html = (await skill.read(ctx, "INBOX:7")).content
    assert "Herbst\n\nAlles über Pilze." in html and "x()" not in html
    assert (await skill.read(ctx, "Spam:1")).is_error

    got = await skill.get_attachment(ctx, "INBOX:3", "liste.txt")
    assert "Pilze\nKastanien" in got.content and got.display["attachment"]["bytes"] == 16
    missing = await skill.get_attachment(ctx, "INBOX:3", "x.pdf")
    assert missing.is_error and "liste.txt" in missing.content
    folders = (await skill.list_folders(ctx)).content
    assert folders == ("INBOX: 3 messages, 1 unread\nEntwürfe: 0 messages, 0 unread\n"
                       "Papierkorb: 0 messages, 0 unread")

    # the one read message is read now; nothing else changed
    assert {m[0]: m[1] for m in FakeIMAP.box["INBOX"]} == {3: {"\\Seen"}, 7: {"\\Seen"},
                                                           9: set()}
    assert [c for c in FakeIMAP.log if any(w in c.upper() for w in (
        "STORE", "COPY", "MOVE", "EXPUNGE"))] == ["UID STORE 3 +FLAGS.SILENT (\\Seen)"]
    assert all("BODY.PEEK" in c for c in FakeIMAP.log if "FETCH" in c.upper())


async def test_a_long_attachment_in_a_folder_reads_on_by_its_listed_name(api):
    skill, ctx = await _ctx(api)
    long = "\n".join(f"Zeile {i}: Pilze und Kastanien im Wald." for i in range(3000))
    FakeIMAP.box["INBOX"][0][2] = _msg("Anna <anna@example.com>", "Scans",
                                       "Fri, 02 Oct 2026 18:30:00 +0200", "Hier.",
                                       attach=("scans/liste.txt", long))
    first = (await skill.get_attachment(ctx, "INBOX:3", "scans/liste.txt")).content
    assert "name='scans/liste.txt', part=2" in first, first[-300:]
    assert not (await skill.get_attachment(ctx, "INBOX:3", "scans/liste.txt", part=2)).is_error


@pytest.mark.parametrize("caps", ["IMAP4rev1 AUTH=PLAIN MOVE UIDPLUS", "IMAP4rev1 AUTH=PLAIN"])
async def test_she_marks_moves_and_deletes(api, caps):
    FakeIMAP.caps = caps                  # the second: COPY, then delete, then EXPUNGE
    skill, ctx = await _ctx(api)
    inbox = lambda: {m[0]: m[1] for m in FakeIMAP.box["INBOX"]}   # noqa: E731

    r = await skill.mark(ctx, ["INBOX:3", "INBOX:9", "INBOX:99"], "read")
    assert r.content == ("2 messages marked read: INBOX:3, INBOX:9. "
                         "Not there anymore: INBOX:99.") and not r.is_error
    assert inbox() == {3: {"\\Seen"}, 7: {"\\Seen"}, 9: {"\\Seen"}}
    await skill.mark(ctx, ["INBOX:9"], "unread")
    await skill.mark(ctx, ["INBOX:9"], "flagged")
    assert inbox()[9] == {"\\Flagged"}
    assert (await skill.mark(ctx, ["INBOX:9"], "eaten")).is_error
    assert (await skill.prompt_fragment(ctx)).endswith("1 unread.")    # not a stale count

    r = await skill.move(ctx, ["INBOX:9"], "Rechnungen")
    assert r.content == "1 message moved to Rechnungen: INBOX:9. The folder Rechnungen is new."
    assert set(inbox()) == {3, 7} and FakeIMAP.box["Rechnungen"][0][1] == {"\\Flagged"}

    r = await skill.delete(ctx, ["INBOX:3", "Rechnungen:1"])
    assert r.content == "2 messages moved to Papierkorb: INBOX:3, Rechnungen:1."
    assert set(inbox()) == {7} and len(FakeIMAP.box["Papierkorb"]) == 2
    r = await skill.delete(ctx, ["Papierkorb:1", "INBOX:7"])
    assert r.content == ("1 message moved to Papierkorb: INBOX:7. "
                         "Already in Papierkorb: Papierkorb:1.")
    assert len(FakeIMAP.box["Papierkorb"]) == 3 and inbox() == {}, "its delete never purges"

    del FakeIMAP.box["Papierkorb"]
    FakeIMAP.box["INBOX"].append([20, set(), _html_only()])
    r = await skill.delete(ctx, ["INBOX:20"])
    assert r.is_error and r.content == "This account has no trash folder." \
        and set(inbox()) == {20}
