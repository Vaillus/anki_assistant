"""Sidecar cache for PDF text extraction. Spec: specs/sources.md.

Each PDF gets a sidecar file (``Author.pdf.md``) next to it, containing:

- A first-line HTML comment with JSON metadata (version, mtime, page count,
  table of contents, list of extracted pages).
- ``--- page N ---`` delimited sections of extracted Markdown.

The sidecar is built lazily on first read and extended incrementally when new
pages are requested.  When Docling is installed, extraction produces
high-quality Markdown; otherwise pypdf text is used as a fallback.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

import pypdf

log = logging.getLogger(__name__)

HAS_DOCLING = find_spec("docling") is not None

_SIDECAR_VERSION = 1
_PAGE_SEP_RE = re.compile(r"^--- page (\d+) ---$", re.MULTILINE)


@dataclass
class TocEntry:
    page: int
    heading: str


@dataclass
class SidecarMeta:
    mtime: float
    n_pages: int
    toc: list[TocEntry]
    extracted: set[int]


# ---------------------------------------------------------------------------
# sidecar path
# ---------------------------------------------------------------------------


def sidecar_path(pdf_path: Path) -> Path:
    return pdf_path.parent / (pdf_path.name + ".md")


# ---------------------------------------------------------------------------
# structural index (table of contents)
# ---------------------------------------------------------------------------


def build_toc(reader: pypdf.PdfReader) -> list[TocEntry]:
    toc = _toc_from_outline(reader)
    if toc:
        return toc
    return _toc_from_heuristic(reader)


def _toc_from_outline(reader: pypdf.PdfReader) -> list[TocEntry]:
    outline = reader.outline
    if not outline:
        return []
    entries: list[TocEntry] = []
    _walk_outline(reader, outline, entries)
    return entries


def _walk_outline(
    reader: pypdf.PdfReader,
    items: list,  # type: ignore[type-arg]
    out: list[TocEntry],
) -> None:
    for item in items:
        if isinstance(item, list):
            _walk_outline(reader, item, out)
        else:
            try:
                page = reader.get_destination_page_number(item)
            except Exception:
                continue
            title = str(item.title).strip()
            if title and page is not None:
                out.append(TocEntry(page=page + 1, heading=title))


def _toc_from_heuristic(reader: pypdf.PdfReader, max_pages: int = 30) -> list[TocEntry]:
    entries: list[TocEntry] = []
    limit = min(len(reader.pages), max_pages)
    for i in range(limit):
        text = (reader.pages[i].extract_text() or "").strip()
        if not text:
            continue
        first_line = text.split("\n", 1)[0].strip()
        if 3 <= len(first_line) <= 120 and not first_line.endswith("."):
            entries.append(TocEntry(page=i + 1, heading=first_line))
    return entries


# ---------------------------------------------------------------------------
# sidecar I/O
# ---------------------------------------------------------------------------


def read_sidecar(pdf_path: Path) -> tuple[SidecarMeta, dict[int, str]] | None:
    sp = sidecar_path(pdf_path)
    if not sp.exists():
        return None
    text = sp.read_text(encoding="utf-8")
    if not text.startswith("<!-- "):
        return None
    end = text.index(" -->")
    raw = text[5:end]
    try:
        header = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if header.get("v") != _SIDECAR_VERSION:
        return None
    current_mtime = pdf_path.stat().st_mtime
    if abs(header["mtime"] - current_mtime) > 0.01:
        return None
    toc = [TocEntry(page=p, heading=h) for p, h in header.get("toc", [])]
    extracted = set(header.get("extracted", []))
    meta = SidecarMeta(
        mtime=header["mtime"],
        n_pages=header["pages"],
        toc=toc,
        extracted=extracted,
    )
    pages = _parse_page_sections(text)
    return meta, pages


def _parse_page_sections(text: str) -> dict[int, str]:
    pages: dict[int, str] = {}
    parts = _PAGE_SEP_RE.split(text)
    i = 1
    while i < len(parts) - 1:
        page_num = int(parts[i])
        content = parts[i + 1].strip("\n")
        pages[page_num] = content
        i += 2
    return pages


def write_sidecar(pdf_path: Path, meta: SidecarMeta, pages: dict[int, str]) -> None:
    header = {
        "v": _SIDECAR_VERSION,
        "mtime": meta.mtime,
        "pages": meta.n_pages,
        "toc": [[e.page, e.heading] for e in meta.toc],
        "extracted": sorted(meta.extracted),
    }
    lines = [f"<!-- {json.dumps(header, ensure_ascii=False)} -->"]
    for p in sorted(pages):
        lines.append("")
        lines.append(f"--- page {p} ---")
        lines.append("")
        lines.append(pages[p])
    content = "\n".join(lines) + "\n"
    sp = sidecar_path(pdf_path)
    tmp = sp.parent / (sp.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(sp)


# ---------------------------------------------------------------------------
# page extraction
# ---------------------------------------------------------------------------


def _extract_docling(pdf_path: Path, page_numbers: list[int]) -> dict[int, str]:
    if not HAS_DOCLING:
        return {}
    from docling.document_converter import DocumentConverter  # ty: ignore[unresolved-import]

    converter = DocumentConverter()
    result: dict[int, str] = {}
    for page_num in page_numbers:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            writer = pypdf.PdfWriter()
            reader = pypdf.PdfReader(str(pdf_path))
            writer.add_page(reader.pages[page_num - 1])
            with tmp_path.open("wb") as f:
                writer.write(f)
            doc_result = converter.convert(str(tmp_path))
            md = doc_result.document.export_to_markdown()
            result[page_num] = md.strip()
        except Exception:
            log.warning("Docling extraction failed for page %d", page_num, exc_info=True)
        finally:
            tmp_path.unlink(missing_ok=True)
    return result


def _extract_pypdf(reader: pypdf.PdfReader, page_numbers: list[int]) -> dict[int, str]:
    result: dict[int, str] = {}
    for p in page_numbers:
        if 1 <= p <= len(reader.pages):
            result[p] = reader.pages[p - 1].extract_text() or ""
    return result


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def get_pdf_toc(pdf_path: Path) -> tuple[int, list[TocEntry]]:
    """Return ``(n_pages, toc)`` quickly, from the sidecar or pypdf outline."""
    cached = read_sidecar(pdf_path)
    if cached is not None:
        meta, _ = cached
        return meta.n_pages, meta.toc
    reader = pypdf.PdfReader(str(pdf_path))
    return len(reader.pages), build_toc(reader)


def get_pdf_text(
    pdf_path: Path,
    pages: str = "",
    prefer_docling: bool = True,
) -> tuple[str, int, list[TocEntry]]:
    """Return ``(assembled_text, n_pages, toc)`` for the requested page range.

    Checks the sidecar cache first.  Extracts missing pages with Docling (when
    available and *prefer_docling* is True) or pypdf, and updates the sidecar.
    """
    from anki_assistant.sources import _page_numbers

    reader = pypdf.PdfReader(str(pdf_path))
    n_pages = len(reader.pages)
    page_nums = _page_numbers(pages, n_pages) if pages else list(range(1, n_pages + 1))

    cached = read_sidecar(pdf_path)
    if cached is not None:
        meta, cached_pages = cached
        toc = meta.toc
    else:
        toc = build_toc(reader)
        mtime = pdf_path.stat().st_mtime
        meta = SidecarMeta(mtime=mtime, n_pages=n_pages, toc=toc, extracted=set())
        cached_pages = {}

    missing = [p for p in page_nums if p not in cached_pages]

    if missing:
        extracted: dict[int, str] = {}
        if prefer_docling and HAS_DOCLING:
            extracted = _extract_docling(pdf_path, missing)
        still_missing = [p for p in missing if p not in extracted]
        if still_missing:
            extracted.update(_extract_pypdf(reader, still_missing))
        cached_pages.update(extracted)
        meta.extracted = meta.extracted | set(extracted)
        write_sidecar(pdf_path, meta, cached_pages)

    blocks = [f"--- page {p} ---\n\n{cached_pages.get(p, '')}" for p in page_nums]
    return "\n\n".join(blocks), n_pages, toc
