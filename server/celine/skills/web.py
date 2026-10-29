"""The web: search through a self-hosted SearXNG, read pages with a local extractor.

Nothing here needs an API key or an account. Search goes to SearXNG (the address in
Settings, Features, Web search: settings/services.py; docker-compose.yml runs one), which
asks the public engines on its behalf without cookies or tracking. `read_page` fetches the
page itself and extracts the readable text with trafilatura (HTML) or pypdf (PDF).

Privacy: a query and a fetched URL leave the machine (SearXNG forwards the query to
the public engines; the page's server sees the fetch). The prompt fragment and the
tool descriptions tell it never to put the Messages archive's contents into a query unless
{{username}} asked it to.

References: every page it sees is numbered once per conversation, `[web:<n>]`, first
line of its block, with its URL on the next line. It cites `[[web:<n>]]`. After the
answer the runner (turns/runner.py `_verify_citations`) checks that every cited number
was listed or read in a tool result in its context (`problems`), and rewrites the good
ones into ordinary Markdown links (`link`), so the stored answer carries the URL.

SSRF: only http(s), only to public addresses. The host is resolved first and every
address must be global (no loopback, private, link-local, CGNAT, multicast, reserved);
redirects are followed by hand and checked the same way; the address the connection
actually went to is checked again before the body is read (DNS rebinding).
"""
from __future__ import annotations

import asyncio
import ipaddress
import mimetypes
import re
import socket
import time
from collections import OrderedDict
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import anyio
import httpx

from ..turns.compact import one_line
from . import ToolContext, ToolResult, guide, knob, tool, described
from ..errors import ToolRefused, unreachable_words
from ..settings import services as services_mod

DEFAULT_UA = ("Mozilla/5.0 (compatible; Celine/0.1; self-hosted personal assistant; "
              "reads pages on request of its user)")
MAX_REDIRECTS = 5
SNIPPET_CHARS = 260
PROBE_EVERY = 60.0                  # seconds a SearXNG check answers for
# Settings, Advanced, read at use (cfg.r)
KNOBS = (
    knob("web.results", "Search results", None, 1, 20, 8,
         "By default; it may ask for up to 20."),
    knob("web.search_timeout", "Search time limit", "seconds", 1, 3600, 15),
    knob("web.read_tokens", "Page part", "tokens", 100, 1_000_000, 6000,
         "A longer page is read in parts."),
    knob("web.timeout", "Page time limit", "seconds", 1, 3600, 20,
         "One fetch, redirects included."),
    knob("web.page_max_mb", "Largest page", "MB", 1, 10_000, 5),
    knob("web.pdf_max_mb", "Largest PDF", "MB", 1, 10_000, 25),
)


def search_down_text(endpoint: str, e: Exception | int) -> str:
    """Why a search failed, one sentence that names SearXNG's address: no connection or
    too slow (an exception), or an answer that is not its JSON (the status)."""
    host = urlsplit(endpoint).netloc or endpoint
    if isinstance(e, int):
        return (f"SearXNG at {host} answered {e} instead of JSON results: fix the address "
                f"so it points at SearXNG, and check that its settings.yml lists json under "
                f"search.formats.")
    if isinstance(e, ValueError):
        return (f"SearXNG at {host} answered something that is not JSON: fix the address "
                f"so it points at SearXNG.")
    return unreachable_words("SearXNG", endpoint, e)


# pages whose metadata is not the page's own: a results page carries the first hit's
# title and a made-up date, a login wall says "Welcome to ..."
_SEARCH_PAGES = re.compile(r"^(?:html\.|lite\.)?duckduckgo\.com/(?:html|lite)|"
                           r"^(?:www\.)?google\.[a-z.]+/search|^(?:www\.)?bing\.com/search")
_LOGIN_PAGES = re.compile(r"^(?:www\.|old\.)?reddit\.com/(?:login|account/login)")


def page_label(url: str) -> str | None:
    """The label of such a page ("Search results", "Login page"), None for others."""
    u = urlsplit(url)
    where = f"{u.netloc.lower()}{u.path}"
    if _SEARCH_PAGES.match(where):
        return "Search results"
    if _LOGIN_PAGES.match(where):
        return "Login page"
    return None

# "[web:3] Title, 2026-09-20" and the URL on the next line: how every block starts,
# and what the verifier reads back out of its context
REF_BLOCK = re.compile(r"(?m)^\[web:(\d+)\][^\n]*\n\s*(https?://\S+)")
CITE_WEB = re.compile(r"\[\[\s*web:(\d+)\s*\]\]")
ARG_REF = re.compile(r"^\s*\[{0,2}\s*web:(\d+)\s*\]{0,2}\s*$")

TimeRange = Literal["day", "week", "month", "year"]


# ---- references ------------------------------------------------------------------------

_numbers: OrderedDict[str, dict[str, int]] = OrderedDict()   # conversation -> url -> n
_KEEP = 256


def _canon(url: str) -> str:
    """The URL a page is numbered by: no fragment, lower-case host."""
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


def refs_in(text: str) -> dict[int, str]:
    """{n: url} of every `[web:n]` block in a tool result. The last one wins."""
    return {int(n): url for n, url in REF_BLOCK.findall(text or "")}


async def _seed(ctx: ToolContext) -> dict[str, int]:
    """This conversation's numbering, rebuilt from its stored web results once per
    process (so numbers stay unique after a restart)."""
    cid = ctx.conversation_id
    if cid in _numbers:
        _numbers.move_to_end(cid)
        return _numbers[cid]
    found: dict[str, int] = {}
    store = ctx.store
    if store is not None:
        def fn(c):
            return [r[0] for r in c.execute(
                "SELECT r.content FROM tool_calls tc "
                "JOIN messages a ON a.id = tc.message_id "
                "JOIN messages r ON r.id = tc.result_message_id "
                "WHERE a.conversation_id = ? AND tc.name LIKE 'web\\_\\_%' ESCAPE '\\' "
                "ORDER BY tc.rowid", (cid,))]
        for content in await store.read(fn):
            for n, url in refs_in(content).items():
                found.setdefault(_canon(url), n)
    got = _numbers.setdefault(cid, found)       # a concurrent call may have won
    while len(_numbers) > _KEEP:
        _numbers.popitem(last=False)
    return got


def _number(table: dict[str, int], url: str) -> int:
    key = _canon(url)
    if key not in table:
        table[key] = max(table.values(), default=0) + 1
    return table[key]


def problems(text: str, shown: dict[int, str]) -> list[Any]:
    """verify.Problem for every [[web:n]] that no tool result in its context showed."""
    from ..turns.verify import Problem, citations
    out, seen = [], set()
    for m in citations(text):
        if m[1] != "web":
            continue
        n = int(m[2])
        if n not in shown and n not in seen:
            seen.add(n)
            out.append(Problem(f"web:{n}", "not_shown"))
    return out


def shown(messages: list[dict[str, Any]]) -> dict[int, str]:
    """{n: url} of every web reference in a tool result it has in context."""
    out: dict[int, str] = {}
    for m in messages:
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            out.update(refs_in(m["content"]))
    return out


SINGLE_WEB = re.compile(r"(?<!\[)\[\s*web:(\d+)\s*\](?![\](])")
RANGE_WEB = re.compile(r"\[\[\s*web:(\d+)\s*[-\u2013]\s*\d+\s*\]\]")


def normalize(text: str) -> str:
    """`[web:3]` (single brackets, as the tools print it) and `[[web:3-5]]` become
    `[[web:3]]`, so the verifier sees every web citation."""
    if not text or "web:" not in text:
        return text
    text = RANGE_WEB.sub(lambda m: f"[[web:{m[1]}]]", text)
    return SINGLE_WEB.sub(lambda m: f"[[web:{m[1]}]]", text)


# a web citation as it may write it ([[web:3]], [web:3], [[web:3 | title]]) and the
# 【3†source】 marks some models print (only that shape: 【注意】 is ordinary CJK text)
_ANY_CITE = re.compile(r"[ \t]*(?:\[\[\s*web:\d+[^\]\n]*\]\]|(?<!\[)\[\s*web:\d+\s*\](?![\](])|"
                       r"【\d+[^】\n]*†[^】\n]*】)")
# [web:3](https://...): a link with a citation for its words keeps only the address
_CITE_LINK = re.compile(r"\[\s*web:\d+\s*\]\((https?://[^)\s]+)\)")


def uncite(text: str) -> str:
    """Its answer handed to another conversation without web citation marks: their
    numbers mean something only in its own conversation."""
    if not text or ("web:" not in text and "【" not in text):
        return text
    return _ANY_CITE.sub("", _CITE_LINK.sub(r"\1", text))


def _label(url: str) -> str:
    host = urlsplit(url).hostname or "link"
    return host[4:] if host.startswith("www.") else host


def link(text: str, table: dict[int, str]) -> str:
    """[[web:n]] -> [host](url) for every n in table; the rest are left alone (the
    verifier marks them invalid)."""
    def sub(m: re.Match) -> str:
        url = table.get(int(m[1]))
        if not url:
            return m[0]
        safe = url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
        return f"[{_label(url)}]({safe})"
    out = CITE_WEB.sub(sub, text)
    # two citations side by side stay two links
    return re.sub(r"(\]\(https?://[^)\s]+\))(?=\[)", r"\1 ", out)


# ---- fetching --------------------------------------------------------------------------

def _public(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def _check_url(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) of an http(s) URL it may fetch, else ToolRefused."""
    try:
        p = urlsplit(url.strip())
        port = p.port
    except ValueError:
        raise ToolRefused(f"not a valid URL: {url}") from None
    if p.scheme not in ("http", "https"):
        raise ToolRefused(f"only http and https URLs can be read, not {p.scheme or 'this'}:")
    host = (p.hostname or "").rstrip(".")
    if not host:
        raise ToolRefused("the URL has no host")
    if p.username or p.password:
        raise ToolRefused("URLs with a user name or password are not fetched")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal",
                                            ".lan", ".home.arpa")):
        raise ToolRefused(f"{host} is a local address; only public web pages can be read")
    return p.scheme, host, port or (443 if p.scheme == "https" else 80)


async def _resolve_public(host: str, port: int) -> None:
    try:
        ipaddress.ip_address(host)
        addrs = [host]
    except ValueError:
        try:
            infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise ToolRefused(f"{host} does not resolve") from None
        addrs = [i[4][0] for i in infos]
    bad = [a for a in addrs if not _public(str(a))]
    if bad or not addrs:
        raise ToolRefused(f"{host} points to a private or local address ({bad[0] if bad else '?'}); "
                      f"only public web pages can be read")


def _peer_ok(resp: httpx.Response) -> None:
    stream = resp.extensions.get("network_stream")
    if stream is None:
        return
    try:
        addr = stream.get_extra_info("server_addr")
    except Exception:
        return
    if addr and not _public(str(addr[0])):
        raise ToolRefused(f"the connection went to a private address ({addr[0]})")


class Page:
    def __init__(self, url: str, ctype: str, body: bytes, status: int):
        self.url, self.ctype, self.body, self.status = url, ctype, body, status


async def fetch(url: str, *, ua: str, timeout: float, max_bytes: int,
                pdf_max_bytes: int, transport: httpx.AsyncBaseTransport | None = None,
                check_addresses: bool = True) -> Page:
    """GET a public URL, following redirects by hand, each hop checked."""
    headers = {"User-Agent": ua,
               "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,"
                         "*/*;q=0.5",
               "Accept-Language": "en,de;q=0.8,*;q=0.5"}
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(transport=transport, trust_env=False, headers=headers,
                                 timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
                                 follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _, host, port = _check_url(url)
            if check_addresses:
                await _resolve_public(host, port)
            left = deadline - time.monotonic()
            if left <= 0:
                raise ToolRefused(f"timed out after {timeout:.0f} s")
            with anyio.fail_after(left):
                async with client.stream("GET", url) as resp:
                    if check_addresses:
                        _peer_ok(resp)
                    if resp.is_redirect and resp.headers.get("location"):
                        url = urljoin(str(resp.url), resp.headers["location"])
                        continue
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    limit = pdf_max_bytes if _is_pdf(ctype, url) else max_bytes
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > limit:
                        raise ToolRefused(f"the page is {int(declared) / 1e6:.1f} MB, over "
                                      f"the {limit / 1e6:.0f} MB limit")
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                        if len(buf) > limit:
                            raise ToolRefused(f"the page is over the {limit / 1e6:.0f} MB limit")
                    return Page(str(resp.url), ctype, bytes(buf), resp.status_code)
        raise ToolRefused(f"more than {MAX_REDIRECTS} redirects")


def _is_pdf(ctype: str, url: str) -> bool:
    return ctype == "application/pdf" or (ctype in ("", "application/octet-stream")
                                          and urlsplit(url).path.lower().endswith(".pdf"))


# ---- extraction ------------------------------------------------------------------------

_TEXTUAL = ("text/plain", "text/markdown", "text/csv", "application/json", "text/xml",
            "application/xml", "application/rss+xml", "application/atom+xml")


def extract(page: Page) -> dict[str, Any]:
    """{title, text, date, author, kind, problems} of a fetched page; problems are the
    parts that could not be read. Raises ToolRefused for a type it cannot read."""
    ctype = page.ctype
    if _is_pdf(ctype, page.url) or page.body[:5] == b"%PDF-":
        return _pdf(page.body)
    if ctype in ("text/html", "application/xhtml+xml", "") or \
            page.body[:1024].lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return _html(page.body, page.url)
    if ctype.startswith("text/") or ctype in _TEXTUAL or ctype.endswith("+json"):
        return {"title": None, "text": page.body.decode("utf-8", "replace"),
                "date": None, "author": None, "kind": ctype, "problems": []}
    raise ToolRefused(f"cannot read {ctype or 'this kind of file'} (HTML, PDF and text only)")


def _html(body: bytes, url: str) -> dict[str, Any]:
    import trafilatura
    from trafilatura.settings import use_config
    conf = use_config()
    conf.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")    # no signals: this runs in a thread
    text = trafilatura.extract(body, url=url, output_format="markdown",
                               include_tables=True, include_comments=False,
                               include_links=False, favor_recall=True, config=conf)
    meta, problems = None, []
    try:
        meta = trafilatura.extract_metadata(body.decode("utf-8", "replace"), default_url=url)
    except Exception as e:
        problems.append(f"Its title, date and author could not be read ({e})")
    if not text:
        try:
            text = _plain_html(body)
        except Exception as e:
            problems.append(f"Its text could not be read ({e})")
    return {"title": getattr(meta, "title", None), "text": text or "",
            "date": getattr(meta, "date", None), "author": getattr(meta, "author", None),
            "kind": "html", "problems": problems}


def _plain_html(body: bytes) -> str:
    import lxml.html
    doc = lxml.html.fromstring(body)
    for bad in doc.xpath("//script|//style|//noscript|//template"):
        bad.drop_tree()
    return re.sub(r"\n\s*\n+", "\n\n", doc.text_content()).strip()


def _pdf(body: bytes) -> dict[str, Any]:
    import io

    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(body))
    except Exception as e:
        raise ToolRefused(f"the PDF could not be opened ({type(e).__name__})") from None
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise ToolRefused("the PDF is encrypted") from None
    parts, problems = [], []
    for i, pg in enumerate(reader.pages, 1):
        try:
            t = (pg.extract_text() or "").strip()
        except Exception as e:
            problems.append(f"Page {i} could not be read ({e})")
            continue
        if t:
            parts.append(f"[page {i}]\n{t}")
    title = None
    try:
        title = (reader.metadata or {}).get("/Title")
    except Exception as e:
        problems.append(f"Its title could not be read ({e})")
    if not parts:
        raise ToolRefused(f"the PDF ({len(reader.pages)} pages) has no text layer (scanned?)"
                          + "".join(f". {p}" for p in problems))
    return {"title": str(title) if title else None, "text": "\n\n".join(parts),
            "date": None, "author": None, "kind": f"pdf, {len(reader.pages)} pages",
            "problems": problems}


def split_parts(text: str, part_tokens: int) -> list[str]:
    """The text in parts of about part_tokens, cut between paragraphs where possible."""
    from ..turns import tokens
    total = tokens.estimate(text, "prose")
    if total <= part_tokens:
        return [text]
    chars = max(500, int(len(text) * part_tokens / max(1, total)))
    out, i = [], 0
    while i < len(text):
        end = min(len(text), i + chars)
        if end < len(text):
            cut = max(text.rfind("\n\n", i + chars // 2, end),
                      text.rfind("\n", i + chars // 2, end))
            if cut > i:
                end = cut
        out.append(text[i:end].strip("\n"))
        i = end
    return [p for p in out if p.strip()] or [text]


# a page read once is kept for paging (and a repeat read) for a while
_pages: OrderedDict[str, tuple[float, dict[str, Any], str]] = OrderedDict()
_PAGE_TTL = 900
_PAGE_KEEP = 32


# ---- the skill -------------------------------------------------------------------------

class Web:
    name = "web"
    guide = guide("web")                 # guides/web.md: when to use, privacy, how
    description = described()

    def __init__(self, cfg, transport: httpx.AsyncBaseTransport | None = None,
                 check_addresses: bool = True):
        self.cfg = cfg
        self.transport = transport              # tests: a fake network
        self.check_addresses = check_addresses  # tests: fake hosts do not resolve
        self.search_down: str | None = None     # why search is down, from the last check
        self._checked = float("-inf")

    async def check(self, *, max_age: float = PROBE_EVERY) -> str | None:
        """Whether SearXNG answers, asked again when the last answer is older than
        `max_age` seconds. Settings, Tools shows Web under Needs setup while it does
        not; reading pages still works, so the skill itself stays up."""
        if time.monotonic() - self._checked < max_age:
            return self.search_down
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False,
                                         timeout=5.0) as client:
                r = await client.get(f"{self.endpoint}/healthz")
            self._note(None if r.status_code < 500
                       else search_down_text(self.endpoint, r.status_code))
        except httpx.HTTPError as e:
            self._note(search_down_text(self.endpoint, e))
        return self.search_down

    def _note(self, down: str | None) -> None:
        self.search_down = down
        self._checked = time.monotonic()

    def _down(self, why: str) -> str:
        """Search did not answer: where else to search, when it has a browser."""
        from ..settings import features
        if features.on(self.cfg, "browser"):
            return why.rstrip() + " Your browser is on: search there."
        return why

    @property
    def endpoint(self) -> str:
        return services_mod.url(self.cfg, "searxng") or services_mod.default("searxng")

    async def prompt_fragment(self, ctx: ToolContext) -> str | None:
        u = self.cfg.user
        return (f"Web: `web__search` and `web__read_page` reach the public internet. Use "
                f"them for current events, facts outside {u}'s own data, documentation, or "
                f"when {u} asks you to look something up. Do not use them for {u}'s chats or "
                f"documents. What you send leaves this machine: never put names, numbers or "
                f"message text from {u}'s Messages archive, Documents or your own notes into a "
                f"query or URL unless {u} asked you to. Search snippets are short and can be "
                f"old: read a page before you rely on details from it. In your chat answer, "
                f"cite web sources as [[web:<n>]] with the numbers the tools show, and only "
                f"pages you listed or read. In notes, keep the link next to the fact: a [[web:<n>]] "
                f"there becomes the page's link. In documents, artifacts and messages, write "
                f"the facts in your own words with no citations and no source lists.")

    @tool(open_world=True)
    async def search(
        self, ctx: ToolContext,
        query: Annotated[str, "A few specific words, like a search engine query. Quotes "
                              "work."],
        time_range: Annotated[TimeRange | None, "Only pages from the last day, week, "
                                                "month or year."] = None,
        language: Annotated[str | None, "A language code, such as en, de or en-US. "
                                        "Default: all."] = None,
        limit: Annotated[int | None, "Results, 1 to 20, default 8."] = None,
        page: Annotated[int | None, "The result page, default 1."] = None,
    ) -> ToolResult:
        """Search the public web through a private SearXNG. Use it for current events,
        prices, releases, documentation. `site:docs.python.org` in the query keeps to one
        site. Returns: per result a `[web:n] title, date` line, the URL and a snippet."""
        q = " ".join((query or "").split())
        if not q:
            return ToolResult(content="The query is empty.", is_error=True)
        want = max(1, min(int(limit or self.cfg.r("web.results")), 20))
        params: dict[str, Any] = {"q": q, "format": "json", "pageno": max(1, int(page or 1))}
        if time_range:
            if time_range not in ("day", "week", "month", "year"):
                return ToolResult(content="time_range is day, week, month or year.",
                                  is_error=True)
            params["time_range"] = time_range
        if language:
            params["language"] = language.strip()
        timeout = float(self.cfg.r("web.search_timeout"))
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False,
                                         timeout=timeout) as client:
                r = await client.get(f"{self.endpoint}/search", params=params,
                                     headers={"Accept": "application/json",
                                              "X-Forwarded-For": "127.0.0.1"})
            if r.status_code != 200:
                return ToolResult(content=self._down(search_down_text(self.endpoint,
                                                                      r.status_code)),
                                  is_error=True)
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError("not a results object")
        except (httpx.HTTPError, ValueError) as e:
            self._note(search_down_text(self.endpoint, e))
            return ToolResult(content=self._down(self.search_down), is_error=True)
        self._note(None)

        table = await _seed(ctx)
        seen: set[str] = set()
        rows = []
        for res in data.get("results") or []:
            url = res.get("url") or ""
            if not url.startswith(("http://", "https://")) or _canon(url) in seen:
                continue
            seen.add(_canon(url))
            rows.append(res)
            if len(rows) >= want:
                break

        head = f"web search: {q}"
        extra = [x for x in (f"past {time_range}" if time_range else "",
                             f"language {language}" if language else "",
                             f"page {params['pageno']}" if params["pageno"] > 1 else "") if x]
        if extra:
            head += f" ({', '.join(extra)})"
        lines = [head]
        for a in (data.get("answers") or [])[:2]:
            text = a.get("answer") if isinstance(a, dict) else str(a)
            if text:
                lines.append(f"instant answer: {one_line(str(text), 300)}")
        for box in (data.get("infoboxes") or [])[:1]:
            if box.get("content"):
                lines.append(f"infobox {box.get('infobox') or ''}: "
                             f"{one_line(box['content'], 300)}")
        if not rows:
            dead = [e[0] if isinstance(e, list) else str(e)
                    for e in data.get("unresponsive_engines") or []]
            lines.append("no results" + (f" (engines not answering: {', '.join(dead[:6])})"
                                         if dead else "") + ". Try other words, fewer "
                         "words, or no time_range.")
            return ToolResult(content="\n".join(lines), display={"query": q, "results": []})
        shown_rows = []
        for res in rows:
            url = res["url"]
            n = _number(table, url)
            title = one_line(res.get("title") or _label(url), 140)
            date = _date(res.get("publishedDate"))
            lines.append(f"[web:{n}] {title}" + (f", {date}" if date else ""))
            lines.append(url)
            snip = one_line(res.get("content") or "", SNIPPET_CHARS)
            if snip:
                lines.append(snip)
            shown_rows.append({"ref": f"web:{n}", "title": title, "url": url, "date": date})
        lines.append(f"more: web__search(query=…, page={params['pageno'] + 1}); read one: "
                     f"web__read_page(url=\"web:<n>\")")
        text = "\n".join(lines)
        if ctx.max_output_tokens:
            from ..turns import tokens
            text, _, _ = tokens.cut_to(text, int(ctx.max_output_tokens))
        return ToolResult(content=text, display={"query": q, "results": shown_rows})

    @tool(open_world=True)
    async def read_page(
        self, ctx: ToolContext,
        url: Annotated[str, "An http or https URL, or a reference from web__search such "
                            "as web:3."],
        part: Annotated[int | None, "Which part of a long page, from 1."] = None,
        save: Annotated[bool, "True: keep the file itself (a picture, a PDF, any file) and "
                              "attach it to this conversation for {{username}}, instead "
                              "of reading its text."] = False,
    ) -> ToolResult:
        """Read the main text of one web page or PDF, or with save=true attach the file
        at the URL to this conversation. Use it to check a search result or a link from
        {{username}}, or to hand them a file you found. Local and private addresses are
        refused. Returns: a `[web:n] title` line, the final URL, one part of the text;
        saved, the file's name and size."""
        table = await _seed(ctx)
        raw = (url or "").strip()
        m = ARG_REF.match(raw)
        if m:
            n = int(m[1])
            target = next((u for u, k in table.items() if k == n), None)
            if target is None:
                return ToolResult(content=f"web:{n} is not a reference in this "
                                          f"conversation. Pass the URL.", is_error=True)
            raw = target
        if raw and "://" not in raw and re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", raw, re.I):
            raw = "https://" + raw
        try:
            _check_url(raw)
        except ToolRefused as e:
            return ToolResult(content=f"not read: {e}", is_error=True)
        if save:
            return await self._save(ctx, raw)

        key = _canon(raw)
        now = time.monotonic()
        hit = _pages.get(key)
        if hit and now - hit[0] < _PAGE_TTL:
            _, doc, final = hit
        else:
            page = await self._get(raw, int(self.cfg.r("web.page_max_mb")) * 1024 * 1024,
                                   int(self.cfg.r("web.pdf_max_mb")) * 1024 * 1024)
            if isinstance(page, ToolResult):
                return page
            try:
                doc = await anyio.to_thread.run_sync(extract, page)
            except ToolRefused as e:
                return ToolResult(content=f"not read: {e}", is_error=True)
            final = page.url
            _pages[key] = (now, doc, final)
            _pages[_canon(final)] = (now, doc, final)
            while len(_pages) > _PAGE_KEEP:
                _pages.popitem(last=False)

        n = _number(table, raw)
        table.setdefault(_canon(final), n)
        budget = int(self.cfg.r("web.read_tokens"))
        if ctx.max_output_tokens:
            budget = max(800, min(budget, int(ctx.max_output_tokens) - 200))
        text = (doc.get("text") or "").strip()
        parts = split_parts(text, budget) if text else [""]
        k = max(1, min(int(part or 1), len(parts)))
        if label := page_label(final):
            doc = {**doc, "title": label, "date": None, "author": None}
        title = one_line(doc.get("title") or _label(final), 160)
        about = [x for x in (doc.get("kind") if doc.get("kind") != "html" else "",
                             f"published {doc['date']}" if doc.get("date") else "",
                             f"by {one_line(doc['author'], 60)}" if doc.get("author") else "")
                 if x]
        lines = [f"[web:{n}] {title}" + (f", {', '.join(about)}" if about else ""), final]
        lines += [f"{p}." for p in doc.get("problems") or ()]
        if len(parts) > 1:
            lines.append(f"part {k} of {len(parts)}")
        body = parts[k - 1] if text else "(no readable text found on this page)"
        lines += ["", body, ""]
        nav = []
        if k > 1:
            nav.append(f"earlier: web__read_page(url=\"web:{n}\", part={k - 1})")
        nav.append(f"later: web__read_page(url=\"web:{n}\", part={k + 1})"
                   if k < len(parts) else "end of page")
        lines.append("; ".join(nav))
        out = "\n".join(lines)
        if ctx.max_output_tokens:
            from ..turns import tokens
            out, _, _ = tokens.cut_to(out, int(ctx.max_output_tokens))
        return ToolResult(content=out, display={"ref": f"web:{n}", "url": final,
                                                "title": title, "part": k,
                                                "parts": len(parts)})

    async def _get(self, raw: str, max_bytes: int, pdf_max_bytes: int) -> Page | ToolResult:
        """The page at `raw`, or the failed result that says why not."""
        try:
            page = await fetch(
                raw, ua=DEFAULT_UA, timeout=float(self.cfg.r("web.timeout")),
                max_bytes=max_bytes, pdf_max_bytes=pdf_max_bytes,
                transport=self.transport, check_addresses=self.check_addresses)
        except ToolRefused as e:
            return ToolResult(content=f"not read: {e}", is_error=True)
        except (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError):
            return ToolResult(content=f"could not fetch {raw}: the site did not answer "
                                      f"within {self.cfg.r('web.timeout')} s.",
                              is_error=True)
        except httpx.ConnectError:
            return ToolResult(content=f"could not fetch {raw}: the site refused the "
                                      f"connection.", is_error=True)
        except (httpx.HTTPError, OSError) as e:
            return ToolResult(content=f"could not fetch {raw}: the connection failed "
                                      f"({type(e).__name__}).", is_error=True)
        if page.status >= 400:
            return ToolResult(content=f"could not read {raw}: HTTP {page.status}",
                              is_error=True)
        return page

    async def _save(self, ctx: ToolContext, raw: str) -> ToolResult:
        """The file at `raw` as an attachment of this call (its row shows it), within
        max_attachment_bytes."""
        if ctx.store is None:
            return ToolResult(content="Only a conversation can take a file.", is_error=True)
        from .. import attachments
        limit = int(self.cfg.r("max_attachment_bytes"))
        page = await self._get(raw, limit, limit)
        if isinstance(page, ToolResult):
            return page
        name = Path(unquote(urlsplit(page.url).path.rsplit("/", 1)[-1])).name or \
            urlsplit(page.url).hostname or "file"
        if "." not in name and page.ctype:
            name += mimetypes.guess_extension(page.ctype) or ""
        row = await attachments.save_generated(self.cfg, ctx.store, page.body, name,
                                               page.ctype or None, ctx.tool_call_id,
                                               in_message=False)
        return ToolResult(content=f"Saved {row['filename']} ({attachments.size(row['bytes'])}"
                                  f", {row['media_type']}) from {page.url}, attached to this "
                                  f"conversation for {self.cfg.user}.",
                          display={"url": page.url})


def _date(v: Any) -> str | None:
    if not v:
        return None
    s = str(v)
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m[1] if m else None


async def probe(cfg, services) -> dict[str, str]:
    """Settings, Features: SearXNG at the saved address (Settings, Features, Web search)
    answers /healthz. Never unset: the address has a default."""
    endpoint = services_mod.url(cfg, "searxng") or services_mod.default("searxng")
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2.5) as client:
            r = await client.get(f"{endpoint}/healthz")
    except httpx.HTTPError as e:
        return {"state": "missing", "detail": search_down_text(endpoint, e)}
    if r.status_code >= 500:
        return {"state": "missing", "detail": search_down_text(endpoint, r.status_code)}
    return {"state": "ready", "detail": f"SearXNG at {urlsplit(endpoint).netloc} answers."}
