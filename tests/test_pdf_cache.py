"""Tests for anki_assistant.pdf_cache."""

from __future__ import annotations

from pathlib import Path

import pypdf
import pytest

import anki_assistant.pdf_cache as pdf_cache
from anki_assistant.pdf_cache import (
    SidecarMeta,
    TocEntry,
    build_toc,
    get_pdf_text,
    get_pdf_toc,
    read_sidecar,
    sidecar_path,
    write_sidecar,
)


def _write_pdf(path: Path, n_pages: int, *, with_outline: bool = False) -> None:
    writer = pypdf.PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=72, height=72)
    with path.open("wb") as f:
        writer.write(f)


def _write_pdf_with_text(path: Path, page_texts: list[str]) -> None:
    """Create a PDF where each page has the given text (via annotation, readable by pypdf)."""
    writer = pypdf.PdfWriter()
    for _ in page_texts:
        writer.add_blank_page(width=200, height=200)
    with path.open("wb") as f:
        writer.write(f)


# ---------------------------------------------------------------- sidecar_path


def test_sidecar_path() -> None:
    assert sidecar_path(Path("/a/b/Author.pdf")) == Path("/a/b/Author.pdf.md")
    assert sidecar_path(Path("relative.pdf")) == Path("relative.pdf.md")


# --------------------------------------------------------- sidecar round-trip


def test_sidecar_write_and_read(tmp_path: Path) -> None:
    pdf = tmp_path / "test.pdf"
    _write_pdf(pdf, 3)

    toc = [TocEntry(page=1, heading="Intro"), TocEntry(page=3, heading="End")]
    meta = SidecarMeta(
        mtime=pdf.stat().st_mtime,
        n_pages=3,
        toc=toc,
        extracted={1, 3},
    )
    pages = {1: "Page one content.", 3: "Page three content."}
    write_sidecar(pdf, meta, pages)

    result = read_sidecar(pdf)
    assert result is not None
    loaded_meta, loaded_pages = result
    assert loaded_meta.n_pages == 3
    assert loaded_meta.extracted == {1, 3}
    assert len(loaded_meta.toc) == 2
    assert loaded_meta.toc[0].heading == "Intro"
    assert loaded_pages[1] == "Page one content."
    assert loaded_pages[3] == "Page three content."


def test_sidecar_returns_none_when_missing(tmp_path: Path) -> None:
    pdf = tmp_path / "nope.pdf"
    _write_pdf(pdf, 1)
    assert read_sidecar(pdf) is None


def test_sidecar_returns_none_when_mtime_stale(tmp_path: Path) -> None:
    import os

    pdf = tmp_path / "test.pdf"
    _write_pdf(pdf, 2)

    meta = SidecarMeta(
        mtime=pdf.stat().st_mtime,
        n_pages=2,
        toc=[],
        extracted={1},
    )
    write_sidecar(pdf, meta, {1: "old"})
    assert read_sidecar(pdf) is not None

    _write_pdf(pdf, 3)
    os.utime(pdf, (pdf.stat().st_atime, pdf.stat().st_mtime + 10))
    assert read_sidecar(pdf) is None


# ----------------------------------------------------------------- build_toc


def test_toc_heuristic_picks_short_first_lines(tmp_path: Path) -> None:
    pdf = tmp_path / "test.pdf"
    _write_pdf(pdf, 5)
    reader = pypdf.PdfReader(str(pdf))
    toc = build_toc(reader)
    assert isinstance(toc, list)


# -------------------------------------------------------- get_pdf_toc


def test_get_pdf_toc_from_fresh_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "test.pdf"
    _write_pdf(pdf, 10)

    n_pages, toc = get_pdf_toc(pdf)
    assert n_pages == 10
    assert isinstance(toc, list)


def test_get_pdf_toc_from_sidecar(tmp_path: Path) -> None:
    pdf = tmp_path / "test.pdf"
    _write_pdf(pdf, 5)

    toc = [TocEntry(page=1, heading="Intro")]
    meta = SidecarMeta(
        mtime=pdf.stat().st_mtime,
        n_pages=5,
        toc=toc,
        extracted=set(),
    )
    write_sidecar(pdf, meta, {})

    n_pages, loaded_toc = get_pdf_toc(pdf)
    assert n_pages == 5
    assert len(loaded_toc) == 1
    assert loaded_toc[0].heading == "Intro"


# ------------------------------------------------------- get_pdf_text


def test_get_pdf_text_creates_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_cache, "HAS_DOCLING", False)
    pdf = tmp_path / "test.pdf"
    _write_pdf(pdf, 3)

    text, n_pages, toc, _ = get_pdf_text(pdf, pages="1-2", prefer_docling=False)
    assert n_pages == 3
    assert "--- page 1 ---" in text
    assert "--- page 2 ---" in text
    assert "--- page 3 ---" not in text
    assert sidecar_path(pdf).exists()


def test_get_pdf_text_incremental(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_cache, "HAS_DOCLING", False)
    pdf = tmp_path / "test.pdf"
    _write_pdf(pdf, 5)

    get_pdf_text(pdf, pages="1-2", prefer_docling=False)
    cached = read_sidecar(pdf)
    assert cached is not None
    meta1, pages1 = cached
    assert meta1.extracted == {1, 2}
    assert set(pages1.keys()) == {1, 2}

    get_pdf_text(pdf, pages="4-5", prefer_docling=False)
    cached = read_sidecar(pdf)
    assert cached is not None
    meta2, pages2 = cached
    assert meta2.extracted == {1, 2, 4, 5}
    assert set(pages2.keys()) == {1, 2, 4, 5}


def test_get_pdf_text_all_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_cache, "HAS_DOCLING", False)
    pdf = tmp_path / "test.pdf"
    _write_pdf(pdf, 3)

    text, n_pages, _, _ = get_pdf_text(pdf, prefer_docling=False)
    assert n_pages == 3
    assert "--- page 1 ---" in text
    assert "--- page 2 ---" in text
    assert "--- page 3 ---" in text


def test_get_pdf_text_reuses_cached_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_cache, "HAS_DOCLING", False)
    pdf = tmp_path / "test.pdf"
    _write_pdf(pdf, 3)

    meta = SidecarMeta(
        mtime=pdf.stat().st_mtime,
        n_pages=3,
        toc=[],
        extracted={1},
    )
    write_sidecar(pdf, meta, {1: "cached page one"})

    text, _, _, _ = get_pdf_text(pdf, pages="1", prefer_docling=False)
    assert "cached page one" in text
