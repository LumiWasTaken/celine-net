"""Documents: reading text out of files, one function per format. All local.

`extract(path)` returns an `Extracted`: the kind of file, a derived title, page count,
and the text cut into segments (the quotable unit: a paragraph, a heading, a subtitle
cue). Nothing here touches a database; kb/documents.py stores what comes back.

Formats v1: plain text and Markdown, code and data files (as text), HTML, PDF (pypdf),
DOCX (python-docx), SRT/VTT subtitles, EPUB (zip + the HTML reader). Images are
recognised (kind "image") and get their text from the caption queue later. Anything
else is kind "file": stored, searchable by its name and descriptions only.

`EXTRACTOR` is the version of this module's output; bumping it re-reads every file.
"""
from __future__ import annotations

import html
import logging
import posixpath
import re
import time
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

log = logging.getLogger("celine.kb")

EXTRACTOR = "documents-extract 1"
SEG_CHARS = 1200          # a paragraph longer than this is split at sentence ends

NOTE = {".md", ".markdown", ".mdown"}
TEXT = {".txt", ".text", ".log", ".rst", ".org", ".tex", ".nfo"}
DATA = {".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".xml", ".cfg"}
CODE = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".c", ".h", ".cpp", ".hpp",
        ".java", ".kt", ".rb", ".php", ".sh", ".bash", ".zsh", ".fish", ".sql", ".lua",
        ".cs", ".swift", ".scala", ".r", ".m", ".pl", ".hs", ".ex", ".exs", ".css", ".scss",
        ".vue", ".svelte", ".dart", ".zig", ".nix", ".dockerfile", ".mk", ".gradle"}
HTML = {".html", ".htm", ".xhtml"}
SUBS = {".srt", ".vtt"}
IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp", ".tif", ".tiff"}
MAX_TEXT_BYTES = 200 * 1024 * 1024     # a text file larger than this is stored, not read
MB = 1024 * 1024
TOO_LARGE = "too large to read as text"
PART_TOO_LARGE = "a part of it is too large to read"
_clock = time.monotonic


def _setting(key: str) -> int:
    from ..settings import general
    return int(general.value({}, key))


@dataclass(frozen=True)
class Limits:
    """What reading one document may cost (the Advanced settings `document_*`): the
    largest part of a zip-based file (EPUB, DOCX) unpacked, the largest HTML file, and
    the pages and seconds a PDF is read for. The defaults are the settings' defaults."""
    part_bytes: int = field(default_factory=lambda: _setting("document_part_mb") * MB)
    html_bytes: int = field(default_factory=lambda: _setting("document_html_mb") * MB)
    pdf_pages: int = field(default_factory=lambda: _setting("document_pdf_pages"))
    pdf_seconds: float = field(default_factory=lambda: _setting("document_pdf_seconds"))


def limits(cfg) -> Limits:
    """The limits as saved now (`cfg.r`), the defaults without a cfg."""
    if cfg is None:
        return Limits()
    return Limits(part_bytes=int(cfg.r("document_part_mb")) * MB,
                  html_bytes=int(cfg.r("document_html_mb")) * MB,
                  pdf_pages=int(cfg.r("document_pdf_pages")),
                  pdf_seconds=float(cfg.r("document_pdf_seconds")))


@dataclass
class Segment:
    kind: str                      # heading | para | cue | caption | cell
    text: str
    page: int | None = None
    heading: str | None = None     # the nearest heading above
    t_ms: int | None = None        # subtitles: cue start
    speaker: str | None = None


@dataclass
class Extracted:
    kind: str                      # note|text|code|data|html|pdf|docx|subtitles|epub|image|file
    title: str | None = None       # from the file itself (H1, metadata); None = the file name
    pages: int | None = None
    segments: list[Segment] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None       # read, but something is off ("no text layer")


def kind_of(path: Path) -> str:
    s = path.suffix.lower()
    if s in NOTE:
        return "note"
    if s in TEXT:
        return "text"
    if s in DATA:
        return "data"
    if s in CODE or path.name.lower() in ("makefile", "dockerfile"):
        return "code"
    if s in HTML:
        return "html"
    if s == ".pdf":
        return "pdf"
    if s == ".docx":
        return "docx"
    if s in SUBS:
        return "subtitles"
    if s == ".epub":
        return "epub"
    if s in IMAGES:
        return "image"
    return "file"


MIME = {"note": "text/markdown", "text": "text/plain", "code": "text/plain",
        "data": "text/plain", "html": "text/html", "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "subtitles": "text/plain", "epub": "application/epub+zip"}


def extract(path: Path, lim: Limits | None = None) -> Extracted:
    """The file's text, within `lim` (limits(cfg); the defaults when None)."""
    lim = lim or Limits()
    kind = kind_of(path)
    if kind == "file" and _looks_textual(path):
        kind = "text"
    fn = {"note": _markdown, "text": _plain, "code": _code, "data": _code, "html": _html_file,
          "pdf": lambda p: _pdf(p, lim), "docx": lambda p: _docx(p, lim),
          "subtitles": _subtitles, "epub": lambda p: _epub(p, lim)}.get(kind)
    if fn is None:
        return Extracted(kind=kind)
    if kind in ("note", "text", "code", "data", "subtitles") and \
            path.stat().st_size > MAX_TEXT_BYTES or \
            kind == "html" and path.stat().st_size > lim.html_bytes:
        return Extracted(kind=kind, error=TOO_LARGE)
    out = fn(path)
    out.kind = kind
    return out


def _looks_textual(path: Path) -> bool:
    """An unknown suffix whose first 4 KB decode as UTF-8 without control bytes."""
    try:
        with path.open("rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    if not head or b"\0" in head:
        return False
    try:
        s = head.decode("utf-8")
    except UnicodeDecodeError as e:
        if e.start < len(head) - 4:
            return False
        s = head[:e.start].decode("utf-8")
    bad = sum(1 for c in s if ord(c) < 32 and c not in "\n\r\t\f")
    return bad <= len(s) * 0.01


# ---- text -------------------------------------------------------------------------------

def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc).replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n")


_SENT = re.compile(r"(?<=[.!?…])[\"'”»)]*\s+")


def split_long(text: str, limit: int = SEG_CHARS) -> list[str]:
    """A paragraph cut at sentence ends (else at spaces) into pieces of at most `limit`."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    out: list[str] = []
    buf = ""
    for sent in _SENT.split(text):
        while len(sent) > limit:                  # one endless sentence: cut at a space
            cut = sent.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if buf:
                out.append(buf)
                buf = ""
            out.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if buf and len(buf) + 1 + len(sent) > limit:
            out.append(buf)
            buf = sent
        else:
            buf = f"{buf} {sent}".strip() if buf else sent
    if buf:
        out.append(buf)
    return [x for x in out if x]


def paragraphs(text: str, *, page: int | None = None, heading: str | None = None,
               limit: int = SEG_CHARS) -> list[Segment]:
    out: list[Segment] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        for piece in split_long(block, limit):
            out.append(Segment("para", piece, page=page, heading=heading))
    return out


_MD_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _markdown(path: Path) -> Extracted:
    text = read_text(path)
    return markdown_text(text)


def markdown_text(text: str) -> Extracted:
    segs: list[Segment] = []
    heading: str | None = None
    title: str | None = None
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        if buf:
            segs.extend(paragraphs("\n".join(buf), heading=heading))
            buf.clear()

    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            buf.append(line)
            continue
        m = None if in_fence else _MD_HEAD.match(line)
        if m:
            flush()
            heading = m.group(2).strip()
            if title is None and len(m.group(1)) == 1:
                title = heading
            segs.append(Segment("heading", heading, heading=heading))
            continue
        buf.append(line)
    flush()
    return Extracted(kind="note", title=title, segments=segs)


def _plain(path: Path) -> Extracted:
    return Extracted(kind="text", segments=paragraphs(read_text(path)))


def _code(path: Path) -> Extracted:
    return Extracted(kind="code", segments=paragraphs(read_text(path), limit=2000))


# ---- html -------------------------------------------------------------------------------

_BLOCK = {"p", "div", "li", "td", "th", "tr", "br", "section", "article", "blockquote",
          "pre", "dd", "dt", "figcaption", "caption", "table", "ul", "ol", "header",
          "footer", "main", "aside", "nav", "hr"}
_HEADS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP = {"script", "style", "noscript", "template", "svg"}


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []     # (kind, text)
        self.buf: list[str] = []
        self.skip = 0
        self.head: str | None = None
        self.title: str | None = None
        self.in_title = False

    def _flush(self, kind: str = "para") -> None:
        t = " ".join("".join(self.buf).split())
        self.buf = []
        if t:
            self.blocks.append((kind, t))

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        if tag == "title":
            self.in_title = True
            return
        if tag in _HEADS:
            self._flush()
            self.head = tag
        elif tag in _BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        if tag == "title":
            self.in_title = False
            return
        if tag in _HEADS and self.head == tag:
            self._flush("heading")
            self.head = None
        elif tag in _BLOCK:
            self._flush()

    def handle_data(self, data):
        if self.in_title:
            self.title = ((self.title or "") + data).strip() or None
            return
        if not self.skip:
            self.buf.append(data)

    def close(self):
        super().close()
        self._flush()


def html_text(text: str) -> Extracted:
    p = _HTMLText()
    p.feed(text)
    p.close()
    segs: list[Segment] = []
    heading = None
    first_h = None
    for kind, t in p.blocks:
        if kind == "heading":
            heading = t
            first_h = first_h or t
            segs.append(Segment("heading", t, heading=t))
        else:
            segs.extend(Segment("para", x, heading=heading) for x in split_long(t))
    return Extracted(kind="html", title=p.title or first_h, segments=segs)


def _html_file(path: Path) -> Extracted:
    return html_text(read_text(path))


# ---- pdf --------------------------------------------------------------------------------

def _pdf(path: Path, lim: Limits) -> Extracted:
    from pypdf import PdfReader
    try:
        r = PdfReader(str(path))
        if r.is_encrypted:
            try:
                opened = r.decrypt("")
            except Exception:
                opened = False
            if not opened:
                return Extracted(kind="pdf", error="the PDF is encrypted")
        pages = len(r.pages)
    except Exception as e:                     # noqa: BLE001 - a broken file is an answer
        return Extracted(kind="pdf", error=f"cannot read the PDF: {type(e).__name__}")
    title = None
    try:
        meta = r.metadata
        title = (meta.title or "").strip() if meta else None
    except Exception:
        log.debug("pdf title unreadable", exc_info=True, extra={"event": "documents.pdf_title_failed"})
    segs: list[Segment] = []
    start, read = _clock(), 0
    for i, page in enumerate(r.pages, 1):
        # the budget is looked at between pages: one page pypdf spins on runs to its end
        if i > lim.pdf_pages or _clock() - start > lim.pdf_seconds:
            break
        read = i
        try:
            text = page.extract_text() or ""
        except Exception:
            log.debug("pdf page unreadable", exc_info=True,
                      extra={"event": "documents.pdf_page_failed", "page": i})
            continue
        segs.extend(_pdf_page(text, i))
    out = Extracted(kind="pdf", title=title or None, pages=pages, segments=segs)
    if read < pages:
        out.error = f"only the first {read} of {pages} pages were read"
    elif not segs and pages:
        out.error = "this PDF has no text layer"
    return out


def _pdf_page(text: str, page: int) -> list[Segment]:
    """pypdf gives lines, rarely blank lines between paragraphs. Lines are joined into
    paragraphs at blank lines or where a line ends a sentence and is short (the last
    line of a paragraph), then long ones are split."""
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    paras: list[str] = []
    buf: list[str] = []
    width = max((len(x) for x in lines), default=0)
    for ln in lines:
        if not ln:
            if buf:
                paras.append(" ".join(buf))
                buf = []
            continue
        if buf and buf[-1].endswith("-") and ln[:1].islower():
            buf[-1] = buf[-1][:-1] + ln                  # hyphenated line break
        else:
            buf.append(ln)
        if ln[-1:] in ".!?:…" and len(ln) < width * 0.7:
            paras.append(" ".join(buf))
            buf = []
    if buf:
        paras.append(" ".join(buf))
    out: list[Segment] = []
    for p in paras:
        out.extend(Segment("para", x, page=page) for x in split_long(p))
    return out


# ---- docx -------------------------------------------------------------------------------

def _too_large(z: zipfile.ZipFile, lim: Limits) -> bool:
    """A part of the zip unpacks larger than the limit (the size its header names, which
    zipfile holds a part to when it reads it)."""
    return any(i.file_size > lim.part_bytes for i in z.infolist())


def _docx(path: Path, lim: Limits) -> Extracted:
    try:
        with zipfile.ZipFile(path) as z:
            if _too_large(z, lim):
                return Extracted(kind="docx", error=PART_TOO_LARGE)
    except (OSError, zipfile.BadZipFile):
        pass                                   # python-docx says what is wrong below
    try:
        import docx
        d = docx.Document(str(path))
    except Exception as e:                     # noqa: BLE001
        return Extracted(kind="docx", error=f"cannot read the document: {type(e).__name__}")
    title = None
    try:
        title = (d.core_properties.title or "").strip() or None
    except Exception:
        log.debug("docx title unreadable", exc_info=True, extra={"event": "documents.docx_title_failed"})
    segs: list[Segment] = []
    heading = None
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            if style.lower().startswith(("heading", "title", "überschrift", "titel")):
                heading = text
                if title is None and style.lower().startswith(("title", "titel", "heading 1",
                                                               "überschrift 1")):
                    title = text
                segs.append(Segment("heading", text, heading=text))
            else:
                segs.extend(Segment("para", x, heading=heading) for x in split_long(text))
        elif tag == "tbl":
            t = Table(child, d)
            for row in t.rows:
                cells: list[str] = []
                for c in row.cells:
                    ct = " ".join(c.text.split())
                    if ct and (not cells or cells[-1] != ct):      # merged cells repeat
                        cells.append(ct)
                if cells:
                    segs.extend(Segment("cell", x, heading=heading)
                                for x in split_long(" | ".join(cells)))
    return Extracted(kind="docx", title=title, segments=segs)


# ---- subtitles --------------------------------------------------------------------------

_TIME = re.compile(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->")
_VOICE = re.compile(r"^<v(?:\.[^ >]*)?\s+([^>]+)>")
_SPEAKER = re.compile(r"^([A-ZÄÖÜ][\w .'-]{0,30}):\s+(?=\S)")
_TAGS = re.compile(r"</?[^>]+>")


def _subtitles(path: Path) -> Extracted:
    return subtitles_text(read_text(path))


def subtitles_text(text: str) -> Extracted:
    segs: list[Segment] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        t_ms = None
        body: list[str] = []
        for ln in lines:
            m = _TIME.search(ln)
            if m and t_ms is None:
                h, mi, s, ms = m.groups()
                t_ms = ((int(h or 0) * 60 + int(mi)) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0"))
                continue
            if t_ms is None:                  # the cue number, WEBVTT header, NOTE
                continue
            body.append(ln.strip())
        if t_ms is None or not body:
            continue
        raw = " ".join(body)
        speaker = None
        if (m := _VOICE.match(raw)):
            speaker = m.group(1).strip()
        clean = html.unescape(_TAGS.sub("", raw)).strip()
        if speaker is None and (m := _SPEAKER.match(clean)):
            speaker = m.group(1).strip()
            clean = clean[m.end():]
        if clean:
            segs.append(Segment("cue", clean, t_ms=t_ms, speaker=speaker))
    return Extracted(kind="subtitles", segments=segs)


# ---- epub -------------------------------------------------------------------------------

def _epub(path: Path, lim: Limits) -> Extracted:
    try:
        with zipfile.ZipFile(path) as z:
            return _epub_zip(z, lim)
    except (OSError, zipfile.BadZipFile) as e:
        return Extracted(kind="epub", error=f"cannot read the EPUB: {type(e).__name__}")


class _PartTooLarge(Exception):
    pass


def _part(z: zipfile.ZipFile, name: str, lim: Limits) -> str:
    """One part of the zip as text; _PartTooLarge when it unpacks over the limit."""
    if z.getinfo(name).file_size > lim.part_bytes:
        raise _PartTooLarge(name)
    return z.read(name).decode("utf-8", "replace")


def _epub_zip(z: zipfile.ZipFile, lim: Limits) -> Extracted:
    try:
        container = _part(z, "META-INF/container.xml", lim)
        opf_path = re.search(r'full-path="([^"]+)"', container).group(1)
        opf = _part(z, opf_path, lim)
    except _PartTooLarge:
        return Extracted(kind="epub", error=PART_TOO_LARGE)
    except Exception as e:                     # noqa: BLE001
        return Extracted(kind="epub", error=f"cannot read the EPUB: {type(e).__name__}")
    base = posixpath.dirname(opf_path)
    manifest = {}
    for m in re.finditer(r"<item\b[^>]*>", opf):
        tag = m.group(0)
        iid = re.search(r'\bid="([^"]+)"', tag)
        href = re.search(r'\bhref="([^"]+)"', tag)
        if iid and href:
            manifest[iid.group(1)] = posixpath.normpath(posixpath.join(base, html.unescape(
                href.group(1))))
    spine = re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf)
    tm = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", opf, re.S)
    title = html.unescape(tm.group(1)).strip() if tm else None
    segs: list[Segment] = []
    error = None
    for i, ref in enumerate(spine, 1):
        name = manifest.get(ref)
        if not name:
            continue
        try:
            doc = _part(z, name, lim)
        except KeyError:
            continue
        except _PartTooLarge:
            error = PART_TOO_LARGE             # left out; the other chapters are read
            continue
        part = html_text(doc)
        for s in part.segments:
            s.page = i                         # the chapter file, as a "page"
            segs.append(s)
    return Extracted(kind="epub", title=title, pages=len(spine) or None, segments=segs,
                     error=error)
