"""Tests for review.py and web/render.py, driven by an in-memory fake AnkiConnect.

`FakeAnkiClient` subclasses `AnkiClient` and overrides only `invoke()`, so every method of the
real client (including the `multi` batching) is exercised. Nothing here touches a real Anki.
"""

from __future__ import annotations

import re
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from anki_assistant import review
from anki_assistant.client import AnkiClient, AnkiConnectError
from anki_assistant.sources import Source, SourceStore
from anki_assistant.web.render import render_field
from anki_assistant.web.routes_review import router

# --------------------------------------------------------------------- the fake

_TOKENS = re.compile(r'[^\s"]*"[^"]*"[^\s"]*|\S+')


class FakeAnkiClient(AnkiClient):
    """An AnkiConnect stand-in holding a tiny collection in memory."""

    def __init__(self) -> None:
        super().__init__(url="http://fake.invalid")
        self.decks: list[str] = []
        self.notes: dict[int, dict[str, Any]] = {}
        self.cards: dict[int, dict[str, Any]] = {}
        self.models: dict[str, list[str]] = {
            "Cloze": ["Text", "Back Extra"],
            "Basic": ["Front", "Back"],
        }
        self.calls: list[str] = []  # action names, in the order they were invoked
        self._ids = count(1000)

    # -- fixture building -------------------------------------------------

    def add(
        self,
        deck: str,
        *,
        model: str = "Cloze",
        fields: dict[str, str] | None = None,
        tags: list[str] | None = None,
        flags: tuple[int, ...] = (0,),
    ) -> int:
        """Put a note in the collection with one card per entry of `flags`."""
        note_id = next(self._ids)
        card_ids = []
        for flag in flags:
            card_id = next(self._ids)
            self.cards[card_id] = {
                "cardId": card_id,
                "note": note_id,
                "deckName": deck,
                "flags": flag,
            }
            card_ids.append(card_id)
        self.notes[note_id] = {
            "noteId": note_id,
            "modelName": model,
            "tags": list(tags or []),
            "fields": dict(fields or {"Text": "x"}),
            "cards": card_ids,
        }
        self._register(deck)
        return note_id

    def _register(self, deck: str) -> None:
        parts = deck.split("::")
        for cut in range(1, len(parts) + 1):
            name = "::".join(parts[:cut])
            if name not in self.decks:
                self.decks.append(name)

    def flags_of(self, note_id: int) -> list[int]:
        return [self.cards[c]["flags"] for c in self.notes[note_id]["cards"]]

    def decks_of(self, note_id: int) -> list[str]:
        return [self.cards[c]["deckName"] for c in self.notes[note_id]["cards"]]

    # -- transport --------------------------------------------------------

    def invoke(self, action: str, **params: Any) -> Any:
        self.calls.append(action)
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            raise AnkiConnectError(f"{action}: unsupported action")
        return handler(**params)

    # -- actions ----------------------------------------------------------
    # Method and parameter names mirror AnkiConnect's, camelCase included.

    def _do_multi(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for sub in actions:
            try:
                result = self.invoke(sub["action"], **sub.get("params", {}))
            except AnkiConnectError as exc:
                out.append({"result": None, "error": str(exc)})
            else:
                out.append({"result": result, "error": None})
        return out

    def _do_deckNames(self) -> list[str]:
        return list(self.decks)

    def _do_findCards(self, query: str) -> list[int]:
        return [cid for cid, card in self.cards.items() if _matches(card, query)]

    def _do_findNotes(self, query: str) -> list[int]:
        return sorted({self.cards[cid]["note"] for cid in self._do_findCards(query)})

    def _do_cardsInfo(self, cards: list[int]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for cid in cards:
            card = self.cards.get(cid)
            if card is None:
                out.append({})
                continue
            note = self.notes[card["note"]]
            out.append(
                {**card, "modelName": note["modelName"], "fields": _api_fields(note["fields"])}
            )
        return out

    def _do_notesInfo(self, notes: list[int]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for nid in notes:
            note = self.notes.get(nid)
            out.append({} if note is None else {**note, "fields": _api_fields(note["fields"])})
        return out

    def _do_updateNote(self, note: dict[str, Any]) -> None:
        stored = self._note(note["id"])
        if "fields" in note:
            stored["fields"].update(note["fields"])
        if "tags" in note:
            stored["tags"] = list(note["tags"])

    def _do_updateNoteFields(self, note: dict[str, Any]) -> None:
        self._note(note["id"])["fields"].update(note["fields"])

    def _do_addNote(self, note: dict[str, Any]) -> int:
        if note["modelName"] not in self.models:
            raise AnkiConnectError(f"model was not found: {note['modelName']}")
        return self.add(
            note["deckName"],
            model=note["modelName"],
            fields=dict(note["fields"]),
            tags=list(note.get("tags", [])),
        )

    def _do_deleteNotes(self, notes: list[int]) -> None:
        for nid in notes:
            for cid in self._note(nid)["cards"]:
                self.cards.pop(cid, None)
            self.notes.pop(nid, None)

    def _do_changeDeck(self, cards: list[int], deck: str) -> None:
        self._register(deck)
        for cid in cards:
            self.cards[cid]["deckName"] = deck

    def _do_setSpecificValueOfCard(self, card: int, keys: list[str], newValues: list[Any]) -> None:
        for key, value in zip(keys, newValues, strict=True):
            self.cards[card][key] = value

    def _do_modelNames(self) -> list[str]:
        return sorted(self.models)

    def _do_modelFieldNames(self, modelName: str) -> list[str]:
        return list(self.models[modelName])

    def _note(self, note_id: int) -> dict[str, Any]:
        note = self.notes.get(note_id)
        if note is None:
            raise AnkiConnectError(f"note was not found: {note_id}")
        return note


def _api_fields(fields: dict[str, str]) -> dict[str, dict[str, Any]]:
    return {name: {"value": value, "order": i} for i, (name, value) in enumerate(fields.items())}


def _matches(card: dict[str, Any], query: str) -> bool:
    """Support the search syntax review.py actually uses: deck:, flag:, nid:, and negation."""
    for token in _TOKENS.findall(query):
        negated = token.startswith("-")
        key, _, value = (token[1:] if negated else token).partition(":")
        value = value.strip('"')
        if key == "deck":
            ok = card["deckName"] == value or card["deckName"].startswith(f"{value}::")
        elif key == "flag":
            ok = card["flags"] == int(value)
        elif key == "nid":
            ok = card["note"] == int(value)
        else:
            raise AnkiConnectError(f"unsupported search token {token!r}")
        if ok == negated:
            return False
    return True


class FakeStore(SourceStore):
    """A SourceStore backed by nothing on disk."""

    def __init__(self, corpora: dict[str, list[Source]] | None = None) -> None:
        super().__init__(path=Path("/nonexistent/sources.json"))
        self.corpora = dict(corpora or {})


@pytest.fixture
def anki() -> FakeAnkiClient:
    return FakeAnkiClient()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


# ------------------------------------------------------------------- list_decks


def test_deck_counts_count_notes_not_cards_and_roll_up(anki: FakeAnkiClient, store: FakeStore):
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


def test_deck_source_kinds_from_store(anki: FakeAnkiClient):
    anki.add("a::b")
    anki.add("z")
    store = FakeStore({"a": [Source(deck="a", kind="obsidian", target="Note")]})

    by_name = {d.name: d for d in review.list_decks(anki, store)}

    assert by_name["a"].source_kinds == ["obsidian"]
    assert by_name["a::b"].source_kinds == ["obsidian"], "inherited from the parent deck"
    assert by_name["z"].source_kinds == [], "no corpus, own or inherited"


def test_deck_source_kinds_are_deduplicated_in_corpus_order(anki: FakeAnkiClient):
    anki.add("a")
    store = FakeStore(
        {
            "a": [
                Source(deck="a", kind="obsidian", target="N"),
                Source(deck="a", kind="pdf", target="f.pdf"),
                Source(deck="a", kind="obsidian", target="M"),
            ]
        }
    )

    assert review.list_decks(anki, store)[0].source_kinds == ["obsidian", "pdf"]


# ------------------------------------------------------------------- list_notes


def test_note_is_flagged_when_any_card_is(anki: FakeAnkiClient):
    mixed = anki.add("d", flags=(0, 2))
    clean = anki.add("d", flags=(0, 0))

    views = {v.note_id: v for v in review.list_notes(anki, "d").notes}

    assert views[mixed].flagged is True
    assert views[mixed].flag_colors == ["orange"]
    assert views[clean].flagged is False
    assert views[clean].flag_colors == []


def test_queue_lists_sub_decks_flagged_first_then_by_id(anki: FakeAnkiClient):
    first_clean = anki.add("d", flags=(0,))
    flagged_sub = anki.add("d::sub", flags=(1,))
    later_flagged = anki.add("d", flags=(2,))

    result = review.list_notes(anki, "d")

    assert result.total == 3
    assert result.flagged == 2
    assert [n.note_id for n in result.notes] == [flagged_sub, later_flagged, first_clean]
    assert result.notes[0].deck == "d::sub", "deck says where the note actually lives"


def test_reason_is_plain_back_extra_only_for_flagged_notes(anki: FakeAnkiClient):
    flagged = anki.add(
        "d", fields={"Text": "t", "Back Extra": "<b>Trop</b> vague&nbsp;?"}, flags=(2,)
    )
    clean = anki.add("d", fields={"Text": "t", "Back Extra": "ignored"}, flags=(0,))

    views = {v.note_id: v for v in review.list_notes(anki, "d").notes}

    assert views[flagged].reason == "Trop vague ?"
    assert views[clean].reason == ""
    assert views[flagged].fields["Back Extra"] == "<b>Trop</b> vague&nbsp;?", "raw value kept"


def test_get_note_unknown_raises(anki: FakeAnkiClient):
    with pytest.raises(review.NoteNotFound):
        review.get_note(anki, 42)


# -------------------------------------------------------------------- decisions


def test_keep_clears_flags_on_all_cards(anki: FakeAnkiClient):
    note_id = anki.add("d", fields={"Text": "t", "Back Extra": "why"}, flags=(2, 3, 0))

    view = review.keep(anki, note_id)

    assert anki.flags_of(note_id) == [0, 0, 0]
    assert view.flagged is False
    assert view.reason == ""
    assert anki.notes[note_id]["fields"]["Back Extra"] == "why", "Garder changes no field"


def test_keep_batches_the_flag_writes_in_one_round_trip(anki: FakeAnkiClient):
    note_id = anki.add("d", flags=(1, 1, 1))
    anki.calls.clear()

    review.keep(anki, note_id)

    assert anki.calls.count("multi") == 1
    assert anki.calls.count("setSpecificValueOfCard") == 3, "still one write per card, batched"


def test_edit_writes_fields_and_tags_and_can_keep_the_flag(anki: FakeAnkiClient):
    note_id = anki.add("d", fields={"Text": "old", "Back Extra": "why"}, tags=["a"], flags=(2,))

    view = review.edit(anki, note_id, fields={"Text": "new"}, tags=["a", "b"], unflag=False)

    assert anki.notes[note_id]["fields"] == {"Text": "new", "Back Extra": "why"}
    assert view.tags == ["a", "b"]
    assert view.flagged is True, "unflag=False keeps the note in the queue"

    review.edit(anki, note_id, fields={"Back Extra": ""})
    assert anki.flags_of(note_id) == [0]


def test_move_changes_deck_and_unflags(anki: FakeAnkiClient):
    note_id = anki.add("from", flags=(2, 3))

    view = review.move(anki, note_id, "to::sub")

    assert anki.decks_of(note_id) == ["to::sub", "to::sub"]
    assert anki.flags_of(note_id) == [0, 0]
    assert view.deck == "to::sub"
    assert view.flagged is False


def test_create_adds_a_sibling_and_leaves_the_original_alone(anki: FakeAnkiClient):
    original = anki.add("d", flags=(2,))

    view = review.create(anki, "d", "Basic", {"Front": "f", "Back": "b"}, ["phd"])

    assert view.note_id != original
    assert (view.deck, view.model, view.tags) == ("d", "Basic", ["phd"])
    assert view.flagged is False
    assert anki.flags_of(original) == [2], "the original keeps its flag"


def test_delete_removes_the_note(anki: FakeAnkiClient):
    note_id = anki.add("d", flags=(2,))

    review.delete(anki, note_id)

    assert note_id not in anki.notes
    with pytest.raises(review.NoteNotFound):
        review.delete(anki, note_id)


# ------------------------------------------------------------------------ split


def test_split_keeping_the_original_edits_it_and_creates_siblings(anki: FakeAnkiClient):
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


def test_split_fragment_can_override_model_and_tags(anki: FakeAnkiClient):
    note_id = anki.add("d", model="Cloze", tags=["phd"], flags=(2,))

    result = review.split(
        anki,
        note_id,
        original={"fields": {"Text": "kept"}},
        new_notes=[{"model": "Basic", "fields": {"Front": "f", "Back": "b"}, "tags": []}],
    )

    assert result.created[0].model == "Basic"
    assert result.created[0].tags == []


def test_split_without_original_deletes_it_after_creating_the_fragments(anki: FakeAnkiClient):
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

    last_add = len(anki.calls) - 1 - anki.calls[::-1].index("addNote")
    assert last_add < anki.calls.index("deleteNotes"), "creation must succeed before the delete"


def test_split_leaves_the_original_in_place_when_a_fragment_fails(anki: FakeAnkiClient):
    note_id = anki.add("d", flags=(2,))

    with pytest.raises(AnkiConnectError):
        review.split(anki, note_id, original=None, new_notes=[{"model": "Nope", "fields": {}}])

    assert note_id in anki.notes
    assert anki.flags_of(note_id) == [2]


# -------------------------------------------------------------------- rendering


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
        ('<img src="photo.png">', "[image]"),
        ('<img src="photo.png" alt="">', "[image]"),
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
        '<span class="cloze" data-n="12">answer</span>'
    )
    assert render_field("{{c1::a}} and {{c2::b}}") == (
        '<span class="cloze" data-n="1">a</span> and <span class="cloze" data-n="2">b</span>'
    )


def test_render_field_escapes_before_marking_up_clozes():
    """A note cannot inject markup: its own angle brackets survive as text, escaped."""
    assert render_field("{{c1::&lt;script&gt;}}") == (
        '<span class="cloze" data-n="1">&lt;script&gt;</span>'
    )


# --------------------------------------------------------------------------- api


@pytest.fixture
def api(anki: FakeAnkiClient, store: FakeStore) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.anki = anki
    app.state.store = store
    return TestClient(app, raise_server_exceptions=False)


def test_api_decks_and_notes(api: TestClient, anki: FakeAnkiClient):
    note_id = anki.add("d", fields={"Text": "a{{c1::b}}", "Back Extra": "why"}, flags=(2,))

    decks = api.get("/api/decks").json()
    assert {"name": "d", "flagged_total": 1}.items() <= decks[0].items()

    payload = api.get("/api/notes", params={"deck": "d"}).json()
    assert payload["flagged"] == 1
    note = payload["notes"][0]
    assert note["note_id"] == note_id
    assert note["fields"]["Text"] == "a{{c1::b}}", "raw value for editing"
    assert note["fields_html"]["Text"] == 'a<span class="cloze" data-n="1">b</span>'
    assert note["reason"] == "why"


def test_api_decisions(api: TestClient, anki: FakeAnkiClient):
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


def test_api_models(api: TestClient):
    assert api.get("/api/models").json() == {
        "Basic": ["Front", "Back"],
        "Cloze": ["Text", "Back Extra"],
    }


def test_api_maps_anki_failures(api: TestClient, anki: FakeAnkiClient, monkeypatch):
    def unreachable(action: str, **params: object) -> object:
        raise AnkiConnectError("Cannot reach AnkiConnect at http://fake.invalid. Is Anki running?")

    monkeypatch.setattr(anki, "invoke", unreachable)
    assert api.get("/api/decks").status_code == 503

    def broken(action: str, **params: object) -> object:
        raise AnkiConnectError("deckNames: collection is not available")

    monkeypatch.setattr(anki, "invoke", broken)
    response = api.get("/api/decks")
    assert response.status_code == 502
    assert "collection is not available" in response.json()["detail"]
