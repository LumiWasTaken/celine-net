"""Reading text out of documents within ceilings (kb/documents_extract.py): an EPUB or
DOCX part that unpacks too large, an HTML file too large, and a PDF past its page or
time budget keep what fits and say why."""
from __future__ import annotations

import zipfile
from types import SimpleNamespace

from celine.kb import documents_extract as dx
from celine.settings import general

MB = 1024 * 1024


def _pdf(path, pages: int) -> None:
    """A small PDF with one line of text per page, its xref offsets exact."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [%s] /Count %d >>" % (
                " ".join(f"{4 + 2 * i} 0 R" for i in range(pages)), pages),
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i in range(pages):
        stream = f"BT /F1 12 Tf 72 712 Td (Page number {i + 1} here.) Tj ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n" \
        .encode()
    path.write_bytes(out)


def _epub(path, chapters: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("META-INF/container.xml",
                   '<container><rootfiles><rootfile full-path="o.opf"/></rootfiles>'
                   '</container>')
        items = "".join(f'<item id="{n}" href="{n}.html"/>' for n in chapters)
        refs = "".join(f'<itemref idref="{n}"/>' for n in chapters)
        z.writestr("o.opf", f"<package><manifest>{items}</manifest>"
                            f"<spine>{refs}</spine></package>")
        for n, body in chapters.items():
            z.writestr(f"{n}.html", body)


def test_the_limits_come_from_the_advanced_settings():
    assert dx.limits(None) == dx.limits(SimpleNamespace(
        r=lambda k: general.value({}, k)))
    lim = dx.limits(SimpleNamespace(r=lambda k: {"document_part_mb": 2, "document_html_mb": 3,
                                                 "document_pdf_pages": 4,
                                                 "document_pdf_seconds": 5}[k]))
    assert lim == dx.Limits(part_bytes=2 * MB, html_bytes=3 * MB, pdf_pages=4, pdf_seconds=5)
    assert dx.limits(None).part_bytes >= 100 * MB, "generous by default"


def test_an_epub_part_that_unpacks_too_large_is_left_out(tmp_path):
    p = tmp_path / "b.epub"
    big = b"<p>" + b"a" * (2 * MB) + b"</p>"           # deflates to a few KB
    _epub(p, {"one": b"<p>Chapter one.</p>", "two": big, "three": b"<p>Chapter three.</p>"})
    got = dx.extract(p, dx.Limits(part_bytes=MB))
    assert [s.text for s in got.segments] == ["Chapter one.", "Chapter three."]
    assert got.error == "a part of it is too large to read"
    whole = dx.extract(p)
    assert {s.page for s in whole.segments} == {1, 2, 3} and whole.error is None


def test_a_docx_part_that_unpacks_too_large_is_not_read(tmp_path):
    p = tmp_path / "w.docx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"<w/>" + b" " * (2 * MB))
    got = dx.extract(p, dx.Limits(part_bytes=MB))
    assert got.kind == "docx" and got.error == "a part of it is too large to read"


def test_an_html_file_too_large_is_not_read(tmp_path):
    p = tmp_path / "page.html"
    p.write_text("<p>hi</p>" + " " * (2 * MB))
    assert dx.extract(p, dx.Limits(html_bytes=MB)).error == "too large to read as text"
    assert dx.extract(p).segments[0].text == "hi"


def test_a_pdf_stops_at_its_page_and_time_budget(tmp_path, monkeypatch):
    p = tmp_path / "d.pdf"
    _pdf(p, 5)
    whole = dx.extract(p)
    assert whole.pages == 5 and [s.page for s in whole.segments] == [1, 2, 3, 4, 5]
    got = dx.extract(p, dx.Limits(pdf_pages=2))
    assert [s.page for s in got.segments] == [1, 2]
    assert got.pages == 5 and got.error == "only the first 2 of 5 pages were read"
    ticks = iter(range(100))
    monkeypatch.setattr(dx, "_clock", lambda: float(next(ticks)))   # a second per look
    got = dx.extract(p, dx.Limits(pdf_seconds=3))
    assert [s.page for s in got.segments] == [1, 2, 3]
    assert got.error == "only the first 3 of 5 pages were read"
