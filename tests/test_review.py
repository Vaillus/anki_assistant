"""Tests for review.py and web/render.py, on a temporary collection (tests/anki_fixture.py):
the real `AnkiClient` over an empty collection created in the test's temp folder. Nothing here
touches the user's collection.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from anki.collection import Collection
from fastapi import FastAPI
from fastapi.testclient import TestClient

from anki_assistant import review
from anki_assistant.client import AnkiError, CollectionLocked
from anki_assistant.sources import Source, SourceStore
from anki_assistant.web.render import render_field
from anki_assistant.web.routes_review import router
from tests.anki_fixture import TempAnki


class FakeStore(SourceStore):
    """A SourceStore backed by nothing on disk."""

    def __init__(self, corpora: dict[str, list[Source]] | None = None) -> None:
        super().__init__(path=Path("/nonexistent/sources.json"))
        self.corpora = dict(corpora or {})


@pytest.fixture
def anki(tmp_path: Path) -> TempAnki:
    return TempAnki(tmp_path)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


# ------------------------------------------------------------------- list_decks


def test_deck_counts_count_notes_not_cards_and_roll_up(anki: TempAnki, store: FakeStore):
    anki.add("a::b", flags=(2, 3, 0))  # one note, three cards, two of them flagged
    anki.add("a::c", flags=(1,))
    anki.add("a::b", flags=(0, 0))  # unflagged, must not count
    anki.add("z", flags=(0,))

    by_name = {d.name: d for d in review.list_decks(anki, store)}

    assert by_name["a::b"].flagged_own == 1, "3 flagged cards of 1 note is 1 flagged note"
    assert by_name["a::c"].flagged_own == 1
    assert by_name["a"].flagged_own == 0, "no card lives directly in 'a'"
    assert by_name["a"].flagged_total == 2, "rolled up over a::b and a::c"
    assert by_name["a::b"].flagged_total == 1
    assert by_name["z"].flagged_total == 0
    assert by_name["a::b"].leaf == "b"
    assert by_name["a::b"].depth == 1


def test_deck_source_kinds_from_store(anki: TempAnki):
    anki.add("a::b")
    anki.add("z")
    store = FakeStore({"a": [Source(deck="a", kind="obsidian", target="Note")]})

    by_name = {d.name: d for d in review.list_decks(anki, store)}

    assert by_name["a"].source_kinds == ["obsidian"]
    assert by_name["a::b"].source_kinds == ["obsidian"], "inherited from the parent deck"
    assert by_name["z"].source_kinds == [], "no corpus, own or inherited"


def test_deck_source_kinds_are_deduplicated_in_corpus_order(anki: TempAnki):
    anki.add("a")
    store = FakeStore(
        {
            "a": [
                Source(deck="a", kind="obsidian", target="N", id="s1"),
                Source(deck="a", kind="pdf", target="f.pdf", id="s2"),
                Source(deck="a", kind="obsidian", target="M", id="s3"),
            ]
        }
    )

    assert review.list_decks(anki, store)[0].source_kinds == ["obsidian", "pdf"]


# ------------------------------------------------------------------- create_deck


def test_create_deck(anki: TempAnki):
    review.create_deck(anki, "x::y")
    assert "x" in anki.decks
    assert "x::y" in anki.decks


def test_create_deck_rejects_duplicate(anki: TempAnki):
    anki.add("d")
    with pytest.raises(review.DeckAlreadyExists):
        review.create_deck(anki, "d")


def test_delete_deck(anki: TempAnki):
    anki.add("a::b", flags=(1,))
    review.delete_deck(anki, "a::b")
    assert "a::b" not in anki.decks
    assert len(anki.cards) == 0


# ------------------------------------------------------------------- list_notes


def test_note_is_flagged_when_any_card_is(anki: TempAnki):
    mixed = anki.add("d", flags=(0, 2))
    clean = anki.add("d", flags=(0, 0))

    views = {v.note_id: v for v in review.list_notes(anki, "d").notes}

    assert views[mixed].flagged is True
    assert views[mixed].flag_colors == ["orange"]
    assert views[clean].flagged is False
    assert views[clean].flag_colors == []


def test_flagged_cards_carry_the_card_ordinal(anki: TempAnki):
    """The flag is per card; the view keeps which, by ordinal, so the UI can replay c{ord+1}."""
    note_id = anki.add("d", flags=(0, 2, 1))  # c2 and c3 flagged, c1 not

    view = review.get_note(anki, note_id)

    assert [(c.ord, c.flag_color) for c in view.flagged_cards] == [(1, "orange"), (2, "red")]
    assert view.flagged_cards[0].card_id == anki.notes[note_id]["cards"][1]
    assert review.get_note(anki, anki.add("d", flags=(0, 0))).flagged_cards == []


def test_queue_lists_sub_decks_flagged_first_then_by_id(anki: TempAnki):
    first_clean = anki.add("d", flags=(0,))
    flagged_sub = anki.add("d::sub", flags=(1,))
    later_flagged = anki.add("d", flags=(2,))

    result = review.list_notes(anki, "d")

    assert result.total == 3
    assert result.flagged == 2
    assert [n.note_id for n in result.notes] == [flagged_sub, later_flagged, first_clean]
    assert result.notes[0].deck == "d::sub", "deck says where the note actually lives"


def test_reason_is_plain_back_extra_only_for_flagged_notes(anki: TempAnki):
    flagged = anki.add(
        "d", fields={"Text": "t", "Back Extra": "<b>Trop</b> vague&nbsp;?"}, flags=(2,)
    )
    clean = anki.add("d", fields={"Text": "t", "Back Extra": "ignored"}, flags=(0,))

    views = {v.note_id: v for v in review.list_notes(anki, "d").notes}

    assert views[flagged].reason == "Trop vague ?"
    assert views[clean].reason == ""
    assert views[flagged].fields["Back Extra"] == "<b>Trop</b> vague&nbsp;?", "raw value kept"


def test_get_note_unknown_raises(anki: TempAnki):
    with pytest.raises(review.NoteNotFound):
        review.get_note(anki, 42)


# -------------------------------------------------------------------- decisions


def test_keep_clears_flags_on_all_cards(anki: TempAnki):
    note_id = anki.add("d", fields={"Text": "t", "Back Extra": "why"}, flags=(2, 3, 0))

    view = review.keep(anki, note_id)

    assert anki.flags_of(note_id) == [0, 0, 0]
    assert view.flagged is False
    assert view.reason == ""
    assert anki.notes[note_id]["fields"]["Back Extra"] == "why", "Garder changes no field"


def test_keep_clears_every_flag_in_one_write(anki: TempAnki):
    note_id = anki.add("d", flags=(1, 1, 1))
    anki.calls.clear()

    review.keep(anki, note_id)

    assert anki.calls.count("set_flag") == 1


def test_edit_writes_fields_and_tags_and_can_keep_the_flag(anki: TempAnki):
    note_id = anki.add("d", fields={"Text": "old", "Back Extra": "why"}, tags=["a"], flags=(2,))

    view = review.edit(anki, note_id, fields={"Text": "new"}, tags=["a", "b"], unflag=False)

    assert anki.notes[note_id]["fields"] == {"Text": "new", "Back Extra": "why"}
    assert view.tags == ["a", "b"]
    assert view.flagged is True, "unflag=False keeps the note in the queue"

    review.edit(anki, note_id, fields={"Back Extra": ""})
    assert anki.flags_of(note_id) == [0]


def test_move_changes_deck_and_unflags(anki: TempAnki):
    note_id = anki.add("from", flags=(2, 3))

    view = review.move(anki, note_id, "to::sub")

    assert anki.decks_of(note_id) == ["to::sub", "to::sub"]
    assert anki.flags_of(note_id) == [0, 0]
    assert view.deck == "to::sub"
    assert view.flagged is False


def test_create_adds_a_sibling_and_leaves_the_original_alone(anki: TempAnki):
    original = anki.add("d", flags=(2,))

    view = review.create(anki, "d", "Basic", {"Front": "f", "Back": "b"}, ["phd"])

    assert view.note_id != original
    assert (view.deck, view.model, view.tags) == ("d", "Basic", ["phd"])
    assert view.flagged is False
    assert anki.flags_of(original) == [2], "the original keeps its flag"


def test_delete_removes_the_note(anki: TempAnki):
    note_id = anki.add("d", flags=(2,))

    review.delete(anki, note_id)

    assert note_id not in anki.notes
    with pytest.raises(review.NoteNotFound):
        review.delete(anki, note_id)


# ------------------------------------------------------------------------ split


def test_split_keeping_the_original_edits_it_and_creates_siblings(anki: TempAnki):
    note_id = anki.add(
        "d::sub",
        model="Cloze",
        fields={"Text": "everything", "Back Extra": "à splitter"},
        tags=["phd", "rl"],
        flags=(2, 2),
    )

    result = review.split(
        anki,
        note_id,
        original={"fields": {"Text": "part one", "Back Extra": ""}},
        new_notes=[{"fields": {"Text": "part two"}}, {"fields": {"Text": "part three"}}],
    )

    assert result.original is not None
    assert result.original.note_id == note_id, "the original survives, keeping its scheduling"
    assert result.original.fields["Text"] == "part one"
    assert result.original.flagged is False
    assert anki.flags_of(note_id) == [0, 0]

    assert [n.fields["Text"] for n in result.created] == ["part two", "part three"]
    for created in result.created:
        assert created.deck == "d::sub", "same deck as the original"
        assert created.tags == ["phd", "rl"], "same tags as the original"
        assert created.model == "Cloze", "model defaults to the original's"
        assert created.flagged is False


def test_split_fragment_can_override_model_and_tags(anki: TempAnki):
    note_id = anki.add("d", model="Cloze", tags=["phd"], flags=(2,))

    result = review.split(
        anki,
        note_id,
        original={"fields": {"Text": "kept"}},
        new_notes=[{"model": "Basic", "fields": {"Front": "f", "Back": "b"}, "tags": []}],
    )

    assert result.created[0].model == "Basic"
    assert result.created[0].tags == []


def test_split_without_original_deletes_it_after_creating_the_fragments(anki: TempAnki):
    note_id = anki.add("d", tags=["phd"], flags=(2,))
    anki.calls.clear()

    result = review.split(
        anki,
        note_id,
        original=None,
        new_notes=[{"fields": {"Text": "one"}}, {"fields": {"Text": "two"}}],
    )

    assert result.original is None
    assert len(result.created) == 2
    assert note_id not in anki.notes

    last_add = len(anki.calls) - 1 - anki.calls[::-1].index("add_note")
    assert last_add < anki.calls.index("delete_notes"), "creation must succeed before the delete"


def test_split_leaves_the_original_in_place_when_a_fragment_fails(anki: TempAnki):
    note_id = anki.add("d", flags=(2,))

    with pytest.raises(AnkiError):
        review.split(anki, note_id, original=None, new_notes=[{"model": "Nope", "fields": {}}])

    assert note_id in anki.notes
    assert anki.flags_of(note_id) == [2]


# -------------------------------------------------------------------- rendering


_PHOTO = '<img src="/api/media/photo.png" alt="" class="field-img">'


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("a<br>b", "a<br>b"),
        ("a<br />b", "a<br>b"),
        ("<div>a</div><div>b</div>", "a<br>b"),
        ("<li>a</li>", "a"),
        ('<img src="x.png" alt="\\frac{1}{2}">', "\\frac{1}{2}"),
        ("<img src=\"latex.png\" alt='E=mc^2'>", "E=mc^2"),
        ('<img src="photo.png">', _PHOTO),
        ('<img src="photo.png" alt="">', _PHOTO),
        ("<img>", "[image]"),
        ('<img src="http://example.com/x.png">', "[image]"),
        ('<img src="../secret.png">', "[image]"),
        ("caf&eacute; &amp; th&eacute;", "café &amp; thé"),
        ("a &lt;b&gt; c", "a &lt;b&gt; c"),
        ("<b>bold</b>", "bold"),
        ("5 < 6", "5 &lt; 6"),
    ],
)
def test_render_field(raw: str, expected: str):
    assert render_field(raw) == expected


def test_render_field_cloze_markers():
    assert render_field("The {{c1::answer}} here") == (
        'The <span class="cloze" data-n="1">answer</span> here'
    )
    assert render_field("{{c12::answer::the hint}}") == (
        '<span class="cloze" data-n="12" data-hint="the hint">answer</span>'
    )
    assert render_field("{{c1::a}} and {{c2::b}}") == (
        '<span class="cloze" data-n="1">a</span> and <span class="cloze" data-n="2">b</span>'
    )


def test_render_field_escapes_before_marking_up_clozes():
    """A note cannot inject markup: its own angle brackets survive as text, escaped."""
    assert render_field("{{c1::&lt;script&gt;}}") == (
        '<span class="cloze" data-n="1">&lt;script&gt;</span>'
    )


def test_render_field_picture_takes_only_the_file_name():
    """Nothing but the media file name crosses from the note into the emitted tag."""
    raw = """<img src='a b&amp;"c.png' onerror="alert(1)" style="x">"""
    assert render_field(raw) == ('<img src="/api/media/a%20b%26%22c.png" alt="" class="field-img">')


def test_render_field_picture_marker_cannot_be_forged():
    """The internal picture marker uses NUL, which is stripped from the note first."""
    assert render_field("x\x000\x00") == "x0"
    assert render_field('\x000\x00<img src="p.png">') == (
        '0<img src="/api/media/p.png" alt="" class="field-img">'
    )


def test_render_field_picture_in_cloze():
    assert render_field('{{c1::<img src="p.png">::<img src="h.png">}}') == (
        '<span class="cloze" data-n="1" data-hint="[image]">'
        '<img src="/api/media/p.png" alt="" class="field-img"></span>'
    )


# --------------------------------------------------------------------------- api


@pytest.fixture
def api(anki: TempAnki, store: FakeStore) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.anki = anki
    app.state.store = store
    app.state.study_deck = None
    return TestClient(app, raise_server_exceptions=False)


def test_api_decks_and_notes(api: TestClient, anki: TempAnki):
    note_id = anki.add("d", fields={"Text": "a{{c1::b}}", "Back Extra": "why"}, flags=(2,))

    resp = api.get("/api/decks").json()
    decks = resp["decks"]
    assert {"name": "d", "flagged_total": 1}.items() <= decks[0].items()
    assert resp["priority_count"] >= 0

    payload = api.get("/api/notes", params={"deck": "d"}).json()
    assert payload["flagged"] == 1
    note = payload["notes"][0]
    assert note["note_id"] == note_id
    assert note["fields"]["Text"] == "a{{c1::b}}", "raw value for editing"
    assert note["fields_html"]["Text"] == 'a<span class="cloze" data-n="1">b</span>'
    assert note["reason"] == "why"
    assert note["flagged_cards"] == [
        {"card_id": anki.notes[note_id]["cards"][0], "ord": 0, "flag_color": "orange"}
    ]


def test_api_decisions(api: TestClient, anki: TempAnki):
    note_id = anki.add("d", tags=["phd"], flags=(2,))

    assert api.post(f"/api/notes/{note_id}/keep").json()["flagged"] is False

    patched = api.patch(f"/api/notes/{note_id}", json={"fields": {"Text": "new"}}).json()
    assert patched["fields"]["Text"] == "new"

    split = api.post(
        f"/api/notes/{note_id}/split",
        json={"original": {"fields": {"Text": "one"}}, "new_notes": [{"fields": {"Text": "two"}}]},
    ).json()
    assert split["original"]["fields"]["Text"] == "one"
    assert split["created"][0]["tags"] == ["phd"]

    created = api.post(
        "/api/notes", json={"deck": "d", "model": "Basic", "fields": {"Front": "f", "Back": "b"}}
    ).json()
    assert created["model"] == "Basic"

    assert api.post(f"/api/notes/{note_id}/move", json={"deck": "other"}).json()["deck"] == "other"
    assert api.delete(f"/api/notes/{note_id}").status_code == 204
    assert api.get(f"/api/notes/{note_id}").status_code == 404


def test_api_create_deck(api: TestClient, anki: TempAnki):
    resp = api.post("/api/decks", json={"name": "new::child"})
    assert resp.status_code == 201
    assert resp.json() == {"name": "new::child"}
    assert "new::child" in anki.decks

    dup = api.post("/api/decks", json={"name": "new::child"})
    assert dup.status_code == 409


def test_api_delete_deck(api: TestClient, anki: TempAnki):
    anki.add("tmp")
    resp = api.delete("/api/decks", params={"name": "tmp"})
    assert resp.status_code == 204
    assert "tmp" not in anki.decks


def test_api_models(api: TestClient):
    models = api.get("/api/models").json()
    assert models["Basic"] == ["Front", "Back"]
    assert models["Cloze"] == ["Text", "Back Extra"]


def test_api_media_serves_file_with_content_type(api: TestClient, anki: TempAnki):
    anki.media["paste-abc.jpg"] = b"\xff\xd8jpeg"
    resp = api.get("/api/media/paste-abc.jpg")
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8jpeg"
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_api_media_name_is_url_decoded(api: TestClient, anki: TempAnki):
    anki.media["my pic é.png"] = b"png"
    resp = api.get("/api/media/my%20pic%20%C3%A9.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


def test_api_media_missing_is_404(api: TestClient):
    assert api.get("/api/media/nope.png").status_code == 404


@pytest.mark.parametrize("name", ["..", "a%2Fb.png", "..%2F..%2Fetc%2Fpasswd", "a%5Cb.png"])
def test_api_media_rejects_paths(api: TestClient, anki: TempAnki, name: str):
    anki.media["b.png"] = b"x"
    assert api.get(f"/api/media/{name}").status_code == 404


def test_api_maps_anki_failures(api: TestClient, anki: TempAnki, monkeypatch):
    def broken() -> list[str]:
        raise AnkiError("deck_names: collection is not available")

    monkeypatch.setattr(anki, "deck_names", broken)
    response = api.get("/api/decks")
    assert response.status_code == 502
    assert "collection is not available" in response.json()["detail"]


def test_api_answers_503_while_anki_desktop_holds_the_collection(api: TestClient, anki: TempAnki):
    anki.add("d", flags=(1,))
    assert anki.path is not None
    desktop = Collection(str(anki.path))  # Anki desktop with the profile loaded
    try:
        response = api.get("/api/decks")
        assert response.status_code == 503
        assert response.json()["detail"] == "Close Anki desktop first"
        with pytest.raises(CollectionLocked):
            anki.deck_names()
    finally:
        desktop.close()
    assert api.get("/api/decks").status_code == 200, "desktop closed: the app works again"


def test_one_request_opens_the_collection_once(api: TestClient, anki: TempAnki, monkeypatch):
    anki.add("d", flags=(1,))
    opened: list[int] = []
    real_open = anki._open

    def counting_open() -> Collection:
        opened.append(1)
        return real_open()

    monkeypatch.setattr(anki, "_open", counting_open)
    assert api.get("/api/notes", params={"deck": "d"}).status_code == 200
    assert len(opened) == 1


# ---------------------------------------------------------------- priority queue


def test_priority_notes_includes_new_flagged(anki: TempAnki):
    nid = anki.add("d", flags=(1,), card_type=0)  # new card, flagged
    anki.add("d", flags=(0,), card_type=0)  # new card, not flagged — excluded
    result = review.priority_notes(anki)
    assert result.count == 1
    assert result.notes[0].note_id == nid


def test_priority_notes_includes_buried_flagged(anki: TempAnki):
    nid = anki.add("d", flags=(2,), queue=-2, card_type=2)  # user-buried, flagged
    result = review.priority_notes(anki)
    assert result.count == 1
    assert result.notes[0].note_id == nid


def test_priority_notes_includes_due_flagged(anki: TempAnki):
    nid = anki.add("d", flags=(1,), queue=2, card_type=2, rel_due=-1)  # review, flagged, due
    result = review.priority_notes(anki)
    assert result.count == 1
    assert result.notes[0].note_id == nid


def test_priority_notes_empty_when_nothing_matches(anki: TempAnki):
    anki.add("d", flags=(0,), card_type=0)  # new but not flagged
    result = review.priority_notes(anki)
    assert result.count == 0
    assert result.notes == []


def test_priority_notes_cross_deck(anki: TempAnki):
    a = anki.add("deck_a", flags=(1,), card_type=0)
    b = anki.add("deck_b", flags=(3,), queue=-2, card_type=2)
    result = review.priority_notes(anki)
    assert result.count == 2
    ids = {n.note_id for n in result.notes}
    assert ids == {a, b}


def test_priority_due_excluded_when_over_budget(anki: TempAnki):
    """A flagged due card beyond the study deck's review budget is excluded."""
    anki.add("d", flags=(1,), queue=2, card_type=2, rel_due=-30)  # flagged, most overdue
    anki.add("d", flags=(0,), queue=2, card_type=2, rel_due=-20)  # unflagged, 2nd
    excluded = anki.add(
        "d", flags=(1,), queue=2, card_type=2, rel_due=-10
    )  # flagged, 3rd — over budget
    anki.set_limits("d", review=2)
    result = review.priority_notes(anki, study_deck="d")
    assert result.count == 1
    assert result.notes[0].note_id != excluded


def test_priority_due_included_when_within_budget(anki: TempAnki):
    """When total due <= budget, all flagged due cards are included."""
    a = anki.add("d", flags=(1,), queue=2, card_type=2, rel_due=-30)
    b = anki.add("d", flags=(1,), queue=2, card_type=2, rel_due=-20)
    anki.set_limits("d", review=100)
    result = review.priority_notes(anki, study_deck="d")
    assert result.count == 2
    ids = {n.note_id for n in result.notes}
    assert ids == {a, b}


def test_priority_due_excluded_when_budget_zero(anki: TempAnki):
    """A deck with no remaining reviews today excludes all its due cards."""
    anki.add("d", flags=(1,), queue=2, card_type=2, rel_due=-30)
    anki.set_limits("d", review=0)
    result = review.priority_notes(anki, study_deck="d")
    assert result.count == 0


def test_priority_due_no_filtering_without_study_deck(anki: TempAnki):
    """Without a study deck, all flagged due cards are included regardless of budget."""
    anki.add("d", flags=(1,), queue=2, card_type=2, rel_due=-30)
    anki.set_limits("d", review=0)
    result = review.priority_notes(anki)
    assert result.count == 1


def test_api_priority_endpoint(api: TestClient, anki: TempAnki):
    nid = anki.add("d", flags=(1,), card_type=0)
    resp = api.get("/api/notes/priority")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["notes"][0]["note_id"] == nid


# ------------------------------------------------------------- reading for the chat


def test_get_notes_batches_round_trips_and_drops_unknown_ids(anki: TempAnki):
    a = anki.add("d", fields={"Text": "a"}, flags=(1,))
    b = anki.add("d", fields={"Text": "b"})
    anki.calls.clear()
    views = review.get_notes(anki, [a, 999_999, b])
    assert [v.note_id for v in views] == [a, b]
    assert views[0].flagged and views[0].deck == "d"
    assert not views[1].flagged
    assert anki.calls == ["notes_info", "cards_info"]


def test_search_notes_caps_and_puts_flagged_first(anki: TempAnki):
    plain = [anki.add("d", fields={"Text": str(i)}) for i in range(3)]
    flagged = anki.add("d", fields={"Text": "f"}, flags=(2,))
    views = review.search_notes(anki, "deck:d")
    assert [v.note_id for v in views] == [flagged, *plain]
    assert len(review.search_notes(anki, "deck:d", limit=2)) == 2
    assert review.search_notes(anki, "deck:nope") == []


def test_edit_reflag_puts_a_flag_back_for_undo(anki: TempAnki):
    note_id = anki.add("d", flags=(1, 0))
    first_card = anki.notes[note_id]["cards"][0]
    review.edit(anki, note_id, fields={"Text": "y"})
    assert anki.flags_of(note_id) == [0, 0]
    view = review.edit(anki, note_id, fields={"Text": "x"}, unflag=False, reflag=[first_card])
    assert anki.flags_of(note_id) == [1, 0]
    assert view.flagged and view.fields["Text"] == "x"


def test_client_note_type_reads_fields_templates_and_css(anki: TempAnki):
    note_type = anki.note_type("Cloze")
    assert note_type.name == "Cloze"
    assert note_type.fields == ["Text", "Back Extra"]
    assert list(note_type.templates) == ["Cloze"]
    assert note_type.templates["Cloze"]["Front"] == "{{cloze:Text}}"
    assert "{{Back Extra}}" in note_type.templates["Cloze"]["Back"]
    assert ".card" in note_type.css
