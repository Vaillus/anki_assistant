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
    (entry,) = raw["decks"]["a::b"]
    # An entry without id gets one on load and it is written back (specs/sources.md).
    assert len(entry.pop("id")) == 6
    assert entry == {"kind": "obsidian", "target": "note"}
    assert raw["anchors"] == {}


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


# --------------------------------------------------------------- ids and anchors


def _store_with_vault(tmp_path: Path) -> SourceStore:
    store = SourceStore(path=tmp_path / "sources.json")
    (tmp_path / "vault").mkdir()
    store.vault = Vault(name="V", path=tmp_path / "vault")
    return store


def test_set_corpus_assigns_ids_and_by_id_finds_them_across_decks(tmp_path: Path) -> None:
    store = SourceStore(path=tmp_path / "sources.json")
    store.set_corpus("a", [Source(deck="a", kind="obsidian", target="x")])
    store.set_corpus("b", [Source(deck="b", kind="obsidian", target="y", id="fixed1")])
    (x,) = store.corpus("a")
    assert len(x.id) == 6 and x.id.islower()
    assert store.by_id(x.id) is x
    fixed = store.by_id("fixed1")
    assert fixed is not None and fixed.target == "y"
    assert store.by_id("nope") is None


def test_anchors_round_trip_and_reload(tmp_path: Path) -> None:
    store = SourceStore(path=tmp_path / "sources.json")
    store.set_corpus(
        "a",
        [
            Source(deck="a", kind="obsidian", target="x", id="srcaaa"),
            Source(deck="a", kind="obsidian", target="y", id="srcbbb"),
        ],
    )
    assert store.anchors(1) == []
    store.set_anchors(1, ["srcaaa", "srcbbb", "srcaaa"])  # duplicates dropped, order kept
    store.add_anchor(2, "srcbbb")
    store.add_anchor(2, "srcbbb")  # no-op
    assert store.anchors(1) == ["srcaaa", "srcbbb"]
    assert store.anchors(2) == ["srcbbb"]
    assert store.anchors_to("srcbbb") == [1, 2]
    assert store.anchored_note_ids() == [1, 2]

    reloaded = SourceStore(path=tmp_path / "sources.json")
    assert reloaded.anchors(1) == ["srcaaa", "srcbbb"]

    with pytest.raises(KeyError):
        store.set_anchors(3, ["unknown"])
    store.set_anchors(1, [])
    assert store.anchors(1) == []
    assert store.remove_anchors([2, 99]) == 1
    assert store.anchored_note_ids() == []


def test_set_corpus_drops_anchors_to_removed_sources(tmp_path: Path) -> None:
    store = SourceStore(path=tmp_path / "sources.json")
    keep = Source(deck="a", kind="obsidian", target="x", id="keep00")
    gone = Source(deck="a", kind="obsidian", target="y", id="gone00")
    store.set_corpus("a", [keep, gone])
    store.set_anchors(1, ["keep00", "gone00"])
    store.set_anchors(2, ["gone00"])

    _, removed = store.set_corpus("a", [keep])
    assert removed == 2
    assert store.anchors(1) == ["keep00"]
    assert store.anchors(2) == []


# -------------------------------------------------------------- vault writes


def test_create_note_writes_the_file_and_appends_to_the_own_corpus(tmp_path: Path) -> None:
    store = _store_with_vault(tmp_path)
    store.set_corpus("a", [Source(deck="a", kind="pdf", target="p.pdf", id="pdf000")])

    source = store.create_note("a", "maths/kkt.md", "# KKT\n\ncontenu", source_id="new000")
    assert source.id == "new000"
    assert source.kind == "obsidian"
    assert source.target == "maths/kkt"
    assert (tmp_path / "vault" / "maths" / "kkt.md").read_text(
        encoding="utf-8"
    ) == "# KKT\n\ncontenu"
    assert [s.id for s in store.corpus("a")] == ["pdf000", "new000"]

    with pytest.raises(FileExistsError):
        store.create_note("a", "maths/kkt", "again")
    with pytest.raises(ValueError):
        store.create_note("a", "   ", "blank name")


def test_create_note_materialises_an_inherited_corpus_keeping_ids(tmp_path: Path) -> None:
    store = _store_with_vault(tmp_path)
    store.set_corpus("a", [Source(deck="a", kind="obsidian", target="x", id="inh000")])
    store.set_anchors(1, ["inh000"])

    source = store.create_note("a::b", "new", "…")
    own = store.corpora["a::b"]
    assert [s.id for s in own] == ["inh000", source.id]
    assert all(s.deck == "a::b" for s in own)
    assert store.corpus("a") == [Source(deck="a", kind="obsidian", target="x", id="inh000")]
    assert store.anchors(1) == ["inh000"]  # the id survived, so did the anchor


def test_replace_in_note_requires_exactly_one_match(tmp_path: Path) -> None:
    store = _store_with_vault(tmp_path)
    path = tmp_path / "vault" / "n.md"
    path.write_text("alpha beta alpha gamma", encoding="utf-8")
    store.set_corpus(
        "a",
        [
            Source(deck="a", kind="obsidian", target="n", id="note00"),
            Source(deck="a", kind="pdf", target="p.pdf", id="pdf000"),
        ],
    )

    store.replace_in_note("note00", "beta", "BETA")
    assert path.read_text(encoding="utf-8") == "alpha BETA alpha gamma"
    with pytest.raises(ValueError, match="ambigu"):
        store.replace_in_note("note00", "alpha", "x")
    with pytest.raises(ValueError, match="introuvable"):
        store.replace_in_note("note00", "delta", "x")
    with pytest.raises(ValueError, match="Obsidian"):
        store.replace_in_note("pdf000", "a", "b")
    with pytest.raises(KeyError):
        store.replace_in_note("nope00", "a", "b")


# --------------------------------------------------------------------- routes


def _api(tmp_path: Path):  # noqa: ANN202 - TestClient
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from anki_assistant.web import routes_sources

    store = _store_with_vault(tmp_path)
    (tmp_path / "vault" / "n.md").write_text("un passage unique", encoding="utf-8")
    store.set_corpus("a", [Source(deck="a", kind="obsidian", target="n", id="note00")])
    store.set_anchors(7, ["note00"])
    app = FastAPI()
    app.state.store = store
    app.include_router(routes_sources.router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False), store


def test_api_corpus_carries_ids_anchored_and_counts(tmp_path: Path) -> None:
    api, _ = _api(tmp_path)
    body = api.get("/api/sources/corpus", params={"deck": "a::b", "note_id": 7}).json()
    assert body["inherited_from"] == "a"
    assert body["anchored"] == ["note00"]
    assert body["sources"][0]["id"] == "note00"
    assert body["sources"][0]["anchored_count"] == 1
    assert api.get("/api/sources/corpus", params={"deck": "a"}).json()["anchored"] == []


def test_api_put_sources_keeps_ids_and_reports_removed_anchors(tmp_path: Path) -> None:
    api, store = _api(tmp_path)
    body = api.put(
        "/api/sources",
        params={"deck": "a"},
        json=[{"kind": "pdf", "target": "p.pdf"}],
    ).json()
    assert body["removed_anchors"] == 1
    assert len(body["sources"][0]["id"]) == 6
    assert store.anchors(7) == []
    assert (
        api.put(
            "/api/sources/anchors", params={"note_id": 7}, json={"source_ids": ["zz"]}
        ).status_code
        == 404
    )


def test_api_create_vault_note_and_conflicts(tmp_path: Path) -> None:
    api, store = _api(tmp_path)
    res = api.post(
        "/api/sources/notes",
        json={
            "deck": "a",
            "name": "maths/kkt",
            "content": "# KKT",
            "anchor_note_ids": [7, 8],
            "id": "chat00",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["source"]["id"] == "chat00"
    assert res.json()["source"]["text"] == "# KKT"
    assert store.anchors(7) == ["note00", "chat00"]
    assert store.anchors(8) == ["chat00"]
    again = api.post("/api/sources/notes", json={"deck": "a", "name": "maths/kkt", "content": "x"})
    assert again.status_code == 409


def test_api_replace_source_text_maps_errors(tmp_path: Path) -> None:
    api, _ = _api(tmp_path)
    ok = api.patch("/api/sources/note00/text", json={"old": "unique", "new": "modifié"})
    assert ok.status_code == 200
    assert ok.json()["source"]["text"] == "un passage modifié"
    assert (
        api.patch("/api/sources/note00/text", json={"old": "unique", "new": "x"}).status_code == 409
    )
    assert api.patch("/api/sources/nope00/text", json={"old": "a", "new": "b"}).status_code == 404
    # undo = the same call with old and new swapped
    back = api.patch("/api/sources/note00/text", json={"old": "modifié", "new": "unique"})
    assert back.json()["source"]["text"] == "un passage unique"
