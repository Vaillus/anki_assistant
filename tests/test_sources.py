"""Tests for anki_assistant.sources. Spec: specs/sources.md."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pypdf
import pytest

from anki_assistant.sources import Source, SourceStore, Vault, vault_notes


def _write_pdf(path: Path, n_pages: int) -> None:
    writer = pypdf.PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=72, height=72)
    with path.open("wb") as f:
        writer.write(f)


# --------------------------------------------------------------------- store


def test_legacy_single_object_read_and_rewritten_as_list(tmp_path: Path) -> None:
    store_path = tmp_path / "sources.json"
    store_path.write_text(
        json.dumps(
            {
                "vault": {"name": "V", "path": str(tmp_path)},
                "decks": {"a::b": {"kind": "obsidian", "target": "note"}},
            }
        ),
        encoding="utf-8",
    )

    store = SourceStore(path=store_path)
    corpus = store.corpus("a::b")
    assert len(corpus) == 1
    assert corpus[0].kind == "obsidian"
    assert corpus[0].target == "note"

    store.save()
    raw = json.loads(store_path.read_text(encoding="utf-8"))
    assert isinstance(raw["decks"]["a::b"], list)
    assert raw["decks"]["a::b"] == [{"kind": "obsidian", "target": "note"}]


def test_inheritance_from_nearest_ancestor(tmp_path: Path) -> None:
    store = SourceStore(path=tmp_path / "sources.json")
    store.set_corpus(
        "a",
        [
            Source(deck="a", kind="obsidian", target="x"),
            Source(deck="a", kind="pdf", target="y.pdf"),
        ],
    )

    corpus = store.corpus("a::b::c")
    assert [s.target for s in corpus] == ["x", "y.pdf"]
    assert all(s.deck == "a" for s in corpus)

    # a closer ancestor wins
    store.set_corpus("a::b", [Source(deck="a::b", kind="obsidian", target="z")])
    corpus = store.corpus("a::b::c")
    assert [s.target for s in corpus] == ["z"]
    assert corpus[0].deck == "a::b"

    # no corpus anywhere in the chain
    assert store.corpus("other::deck") == []


def test_set_corpus_empty_list_deletes_entry(tmp_path: Path) -> None:
    store = SourceStore(path=tmp_path / "sources.json")
    store.set_corpus("a::b", [Source(deck="a::b", kind="obsidian", target="x")])
    assert "a::b" in store.corpora

    store.set_corpus("a::b", [])
    assert "a::b" not in store.corpora
    assert store.corpus("a::b") == []


def test_get_returns_first_source_of_corpus(tmp_path: Path) -> None:
    store = SourceStore(path=tmp_path / "sources.json")
    assert store.get("a::b") is None

    store.set_corpus(
        "a",
        [
            Source(deck="a", kind="obsidian", target="first"),
            Source(deck="a", kind="obsidian", target="second"),
        ],
    )
    first = store.get("a::b")
    assert first is not None
    assert first.target == "first"


def test_set_replaces_dont_append(tmp_path: Path) -> None:
    store = SourceStore(path=tmp_path / "sources.json")
    store.set_corpus(
        "a",
        [
            Source(deck="a", kind="obsidian", target="first"),
            Source(deck="a", kind="obsidian", target="second"),
        ],
    )
    store.set("a", "replacement")
    assert [s.target for s in store.corpus("a")] == ["replacement"]


def test_broken_lists_missing_targets_across_all_decks(tmp_path: Path) -> None:
    store = SourceStore(path=tmp_path / "sources.json")
    real_note = tmp_path / "vault" / "exists.md"
    real_note.parent.mkdir(parents=True)
    real_note.write_text("hello", encoding="utf-8")
    store.vault = Vault(name="V", path=tmp_path / "vault")

    store.set_corpus(
        "a",
        [
            Source(deck="a", kind="obsidian", target="exists"),
            Source(deck="a", kind="obsidian", target="missing"),
        ],
    )
    broken = store.broken()
    assert len(broken) == 1
    assert broken[0].target == "missing"


# --------------------------------------------------------------------- text


def test_obsidian_text_strips_front_matter(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    note = vault_dir / "Note.md"
    note.write_text("---\ntags:\n  - x\n---\nBody line one.\nBody line two.\n", encoding="utf-8")

    vault = Vault(name="V", path=vault_dir)
    source = Source(deck="d", kind="obsidian", target="Note")
    result = source.text(vault)

    assert "tags:" not in result.text
    assert "---" not in result.text
    assert "Body line one." in result.text
    assert result.n_pages is None
    assert result.warning == ""


def test_obsidian_text_without_front_matter_is_unchanged(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "Plain.md").write_text("Just a body, no front matter.", encoding="utf-8")

    vault = Vault(name="V", path=vault_dir)
    source = Source(deck="d", kind="obsidian", target="Plain")
    result = source.text(vault)
    assert result.text == "Just a body, no front matter."


def test_pdf_page_range_parsing_and_markers(tmp_path: Path) -> None:
    pdf_path = tmp_path / "doc.pdf"
    _write_pdf(pdf_path, 3)

    vault = Vault(name="V", path=tmp_path)
    source = Source(deck="d", kind="pdf", target=str(pdf_path), pages="1-2")
    result = source.text(vault)

    assert result.n_pages == 3
    assert "--- page 1 ---" in result.text
    assert "--- page 2 ---" in result.text
    assert "--- page 3 ---" not in result.text
    assert result.warning == ""  # a page range was given


def test_pdf_whole_document_gets_warning(tmp_path: Path) -> None:
    pdf_path = tmp_path / "doc.pdf"
    _write_pdf(pdf_path, 3)

    vault = Vault(name="V", path=tmp_path)
    source = Source(deck="d", kind="pdf", target=str(pdf_path))  # no pages -> whole doc
    result = source.text(vault)

    assert result.n_pages == 3
    assert "--- page 1 ---" in result.text
    assert "--- page 2 ---" in result.text
    assert "--- page 3 ---" in result.text
    assert "sans plage de pages" in result.warning
    assert "3 pages" in result.warning


def test_pdf_discontinuous_ranges(tmp_path: Path) -> None:
    pdf_path = tmp_path / "doc.pdf"
    _write_pdf(pdf_path, 9)

    vault = Vault(name="V", path=tmp_path)
    source = Source(deck="d", kind="pdf", target=str(pdf_path), pages="3-5,9")
    result = source.text(vault)

    for p in (3, 4, 5, 9):
        assert f"--- page {p} ---" in result.text
    for p in (1, 2, 6, 7, 8):
        assert f"--- page {p} ---" not in result.text


def test_pdf_cache_invalidates_on_mtime_change(tmp_path: Path) -> None:
    pdf_path = tmp_path / "doc.pdf"
    _write_pdf(pdf_path, 2)
    vault = Vault(name="V", path=tmp_path)
    source = Source(deck="d", kind="pdf", target=str(pdf_path))

    first = source.text(vault)
    assert first.n_pages == 2

    # Rewrite with more pages and force a distinct mtime.
    time.sleep(0.01)
    _write_pdf(pdf_path, 4)
    later = time.time() + 5
    os.utime(pdf_path, (later, later))

    second = source.text(vault)
    assert second.n_pages == 4


def test_text_truncates_and_flags_truncated(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "Long.md").write_text("x" * 100, encoding="utf-8")

    vault = Vault(name="V", path=vault_dir)
    source = Source(deck="d", kind="obsidian", target="Long")
    result = source.text(vault, max_chars=10)

    assert result.text == "x" * 10
    assert result.truncated is True


# --------------------------------------------------------------------- uri


def test_uri_percent_encodes_spaces_never_plus(tmp_path: Path) -> None:
    vault = Vault(name="My Vault", path=tmp_path)
    source = Source(deck="d", kind="obsidian", target="A note with spaces")
    uri = source.uri(vault)

    assert "%20" in uri
    assert "+" not in uri
    assert uri.startswith("obsidian://open?vault=My%20Vault&file=A%20note%20with%20spaces")


# --------------------------------------------------------------------- vault_notes


def test_vault_notes_filters_case_insensitive_sorted_and_skips_hidden(tmp_path: Path) -> None:
    (tmp_path / "Alpha.md").write_text("", encoding="utf-8")
    (tmp_path / "beta.md").write_text("", encoding="utf-8")
    (tmp_path / "Gamma Thing.md").write_text("", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "Delta.md").write_text("", encoding="utf-8")
    hidden = tmp_path / ".obsidian"
    hidden.mkdir()
    (hidden / "config.md").write_text("", encoding="utf-8")
    trash = tmp_path / ".trash"
    trash.mkdir()
    (trash / "Old.md").write_text("", encoding="utf-8")

    vault = Vault(name="V", path=tmp_path)

    all_notes = vault_notes(vault, "")
    assert all_notes == sorted(all_notes, key=str.lower)
    assert "sub/Delta" in all_notes
    assert not any("config" in n for n in all_notes)
    assert not any("Old" in n for n in all_notes)
    assert all(not n.endswith(".md") for n in all_notes)

    matches = vault_notes(
        vault, "a"
    )  # case-insensitive: matches Alpha, beta, Gamma Thing, sub/Delta
    assert set(matches) == {"Alpha", "beta", "Gamma Thing", "sub/Delta"}


def test_vault_notes_limit(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"Note{i}.md").write_text("", encoding="utf-8")
    vault = Vault(name="V", path=tmp_path)
    assert len(vault_notes(vault, "", limit=2)) == 2


def test_vault_notes_missing_vault_returns_empty(tmp_path: Path) -> None:
    vault = Vault(name="V", path=tmp_path / "does-not-exist")
    assert vault_notes(vault, "") == []


@pytest.fixture(autouse=True)
def _isolate_pdf_cache():
    """The PDF text cache is process-global; make sure other test modules don't leak into it."""
    import anki_assistant.sources as sources_module

    sources_module._PDF_TEXT_CACHE.clear()
    yield
    sources_module._PDF_TEXT_CACHE.clear()
