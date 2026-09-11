"""Tests for workspace.py and web/routes_workspace.py. Spec: specs/workspace.md.

Driven by the in-memory `FakeAnkiClient` of test_review.py, extended here with failure
injection (`fail_on`: raise on the n-th call of an action) to exercise the rollback paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from anki_assistant import workspace
from anki_assistant.client import AnkiConnectError
from anki_assistant.review import NoteNotFound
from anki_assistant.sources import Source, SourceStore
from anki_assistant.web import routes_review, routes_workspace
from anki_assistant.workspace import ApplyPlan, CardPlan
from tests.test_review import FakeAnkiClient

# --------------------------------------------------------------------------- fixtures


class FailingAnki(FakeAnkiClient):
    """Raises on the n-th invocation of a given action; every other call goes through."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_on: dict[str, int] = {}
        self._seen: dict[str, int] = {}

    def invoke(self, action: str, **params: Any) -> Any:
        self._seen[action] = self._seen.get(action, 0) + 1
        if self.fail_on.get(action) == self._seen[action]:
            self.calls.append(action)
            raise AnkiConnectError(f"{action}: boom")
        return super().invoke(action, **params)


@pytest.fixture
def anki() -> FailingAnki:
    return FailingAnki()


@pytest.fixture
def store(tmp_path: Path) -> SourceStore:
    """A store on disk (anchors save on write) with two sources: `s1` on deck d, `s2` on deck e."""
    st = SourceStore(path=tmp_path / "sources.json")
    st.corpora = {
        "d": [Source(deck="d", kind="obsidian", target="A", id="s1")],
        "e": [Source(deck="e", kind="obsidian", target="B", id="s2")],
    }
    return st


def edit(wid: str, note_id: int, text: str, **extra: Any) -> CardPlan:
    return CardPlan(
        wid=wid, action="edit", note_id=note_id, fields={"Text": text, "Back Extra": "why"}, **extra
    )


def create(wid: str, text: str, **extra: Any) -> CardPlan:
    fields = {"Text": text, "Back Extra": ""}
    return CardPlan(wid=wid, action="create", model="Cloze", fields=fields, **extra)


# ------------------------------------------------------------------------- validate


def test_validate_reports_every_shape_problem() -> None:
    plan = ApplyPlan(
        deck="d",
        cards=[
            CardPlan(wid="", action="keep", note_id=1),
            CardPlan(wid="w1", action="bogus", note_id=1),
            CardPlan(wid="w1", action="create", note_id=3, move_to="x"),
            CardPlan(wid="w2", action="edit", note_id=1),
            CardPlan(wid="w3", action="delete", note_id=1, move_to="x"),
            CardPlan(wid="w4", action="keep"),
        ],
    )
    errors = workspace.validate(plan)
    joined = "\n".join(errors)
    for needle in (
        "carte sans identifiant",
        "identifiant w1 en double",
        "action inconnue",
        "n'a pas de note_id",
        "type de note manquant",
        "champs manquants",
        "ne se déplace pas",
        "apparaît deux fois",
        "note_id manquant",
        "une note supprimée ne se déplace pas",
    ):
        assert needle in joined, needle
    assert workspace.validate(ApplyPlan(deck="d")) == ["plan vide"]
    assert workspace.validate(ApplyPlan(deck="d", cards=[edit("w1", 1, "x")])) == []


def test_apply_refuses_a_malformed_plan_before_touching_anki(anki: FailingAnki, store: SourceStore):
    with pytest.raises(workspace.PlanError):
        workspace.apply(anki, store, ApplyPlan(deck="d"))
    assert anki.calls == []


def test_apply_refuses_an_unknown_note_before_writing(anki: FailingAnki, store: SourceStore):
    with pytest.raises(NoteNotFound):
        workspace.apply(anki, store, ApplyPlan(deck="d", cards=[edit("w1", 999, "x")]))
    assert "updateNote" not in anki.calls


# ---------------------------------------------------------------------------- apply


def test_apply_writes_every_kind_of_card_in_the_safe_order(anki: FailingAnki, store: SourceStore):
    edited = anki.add("d", fields={"Text": "old", "Back Extra": "why"}, flags=(2, 0))
    kept = anki.add("d", fields={"Text": "fine", "Back Extra": "hm"}, flags=(1,))
    gone = anki.add("d", fields={"Text": "bye"}, flags=(3,))
    store.set_anchors(gone, ["s1"])
    plan = ApplyPlan(
        deck="d",
        cards=[
            edit("w1", edited, "new", tags=["t"]),
            create("w2", "frag", parent_wid="w1", deck="d", tags=["t"], source_ids=["s1"]),
            CardPlan(wid="w3", action="keep", note_id=kept, move_to="e"),
            CardPlan(wid="w4", action="delete", note_id=gone),
        ],
    )
    report, snap = workspace.apply(anki, store, plan)

    assert report.ok and report.errors == []
    assert anki.notes[edited]["fields"] == {"Text": "new", "Back Extra": ""}, "reason cleared"
    assert anki.notes[edited]["tags"] == ["t"]
    assert anki.flags_of(edited) == [0, 0]
    assert anki.notes[kept]["fields"]["Back Extra"] == "hm", "kept notes keep their reason"
    assert anki.flags_of(kept) == [0]
    assert anki.decks_of(kept) == ["e"]
    assert gone not in anki.notes
    assert store.anchors(gone) == []
    new_id = report.created["w2"]
    assert anki.notes[new_id]["fields"]["Text"] == "frag"
    assert anki.notes[new_id]["tags"] == ["t"]
    assert store.anchors(new_id) == ["s1"]
    assert anki.flags_of(new_id) == [0]

    assert report.resolved == [edited, kept]
    assert report.moved == [kept]
    assert report.deleted == [gone]
    assert report.undo_available is False, "the validation deleted a note"
    assert snap.deleted == [gone]

    order = [a for a in anki.calls if a in ("addNote", "updateNote", "changeDeck", "deleteNotes")]
    assert order == ["addNote", "updateNote", "changeDeck", "deleteNotes"]
    unflag_at = anki.calls.index("setSpecificValueOfCard")
    assert anki.calls.index("changeDeck") < unflag_at < anki.calls.index("deleteNotes")


def test_clear_reason_off_keeps_back_extra(anki: FailingAnki, store: SourceStore):
    nid = anki.add("d", fields={"Text": "old", "Back Extra": "why"}, flags=(1,))
    plan = ApplyPlan(deck="d", clear_reason=False, cards=[edit("w1", nid, "new")])
    report, _ = workspace.apply(anki, store, plan)
    assert report.ok
    assert anki.notes[nid]["fields"] == {"Text": "new", "Back Extra": "why"}


def test_edit_without_tags_leaves_them_and_edit_only_plan_allows_undo(
    anki: FailingAnki, store: SourceStore
):
    nid = anki.add("d", fields={"Text": "old", "Back Extra": ""}, tags=["k"], flags=(1,))
    report, _ = workspace.apply(anki, store, ApplyPlan(deck="d", cards=[edit("w1", nid, "new")]))
    assert anki.notes[nid]["tags"] == ["k"]
    assert report.undo_available is True


def test_move_drops_anchors_absent_from_the_destination_corpus(
    anki: FailingAnki, store: SourceStore
):
    nid = anki.add("d", flags=(1,))
    store.set_anchors(nid, ["s1"])
    plan = ApplyPlan(deck="d", cards=[CardPlan(wid="w1", action="keep", note_id=nid, move_to="e")])
    workspace.apply(anki, store, plan)
    assert store.anchors(nid) == [], "s1 is not in e's corpus"


# ------------------------------------------------------------------------- rollback


def test_failure_in_the_middle_rolls_back_creates_and_edits(anki: FailingAnki, store: SourceStore):
    a = anki.add("d", fields={"Text": "a0", "Back Extra": "r"}, tags=["x"], flags=(2,))
    b = anki.add("d", fields={"Text": "b0", "Back Extra": ""}, flags=(0,))
    anki.fail_on["updateNote"] = 2  # the second edit fails
    plan = ApplyPlan(
        deck="d",
        cards=[create("w0", "frag", deck="d"), edit("w1", a, "a1"), edit("w2", b, "b1")],
    )
    report, snap = workspace.apply(anki, store, plan)

    assert report.ok is False
    assert report.rolled_back is True
    assert report.created == {}, "the created note was deleted again"
    assert report.resolved == []
    assert any(e.startswith(f"modification de #{b}") for e in report.errors)
    assert anki.notes[a]["fields"] == {"Text": "a0", "Back Extra": "r"}
    assert anki.notes[a]["tags"] == ["x"]
    assert anki.flags_of(a) == [2], "never unflagged: the failure came before"
    assert anki.notes[b]["fields"]["Text"] == "b0"
    assert len(anki.notes) == 2
    assert snap.created == [] and snap.written == {}


def test_failure_at_the_delete_step_restores_flags_moves_and_fields(
    anki: FailingAnki, store: SourceStore
):
    a = anki.add("d", fields={"Text": "a0", "Back Extra": ""}, flags=(4, 0, 1))
    m = anki.add("d", flags=(1,))
    gone = anki.add("d", flags=(1,))
    anki.fail_on["deleteNotes"] = 1
    plan = ApplyPlan(
        deck="d",
        cards=[
            edit("w1", a, "a1"),
            CardPlan(wid="w2", action="keep", note_id=m, move_to="e"),
            CardPlan(wid="w3", action="delete", note_id=gone),
        ],
    )
    report, _ = workspace.apply(anki, store, plan)
    assert report.ok is False and report.rolled_back is True
    assert gone in anki.notes, "deletion is the last step, nothing was lost"
    assert anki.notes[a]["fields"]["Text"] == "a0"
    assert anki.flags_of(a) == [4, 0, 1], "flag colours restored card by card"
    assert anki.flags_of(m) == [1]
    assert anki.decks_of(m) == ["d"]
    assert report.moved == [] and report.deleted == []
    assert report.undo_available is False


def test_rollback_failure_is_reported_and_created_notes_keep_their_ids(
    anki: FailingAnki, store: SourceStore
):
    a = anki.add("d", flags=(1,))
    anki.fail_on["changeDeck"] = 1
    anki.fail_on["deleteNotes"] = 1  # the rollback of the create fails too
    plan = ApplyPlan(
        deck="d",
        cards=[create("w0", "frag", deck="d"), edit("w1", a, "a1", move_to="e")],
    )
    report, _ = workspace.apply(anki, store, plan)
    assert report.ok is False
    assert report.rolled_back is False
    assert list(report.created) == ["w0"], "still in Anki: the client keeps its id"
    assert any("annulation de la création" in e for e in report.errors)
    assert any(e.startswith(f"déplacement de #{a}") for e in report.errors)


# ------------------------------------------------------------------------------ undo


def test_undo_reverts_a_successful_validation(anki: FailingAnki, store: SourceStore):
    a = anki.add("d", fields={"Text": "a0", "Back Extra": "r"}, tags=["x"], flags=(2, 0))
    m = anki.add("d", flags=(1,))
    store.set_anchors(m, ["s1"])
    plan = ApplyPlan(
        deck="d",
        cards=[
            create("w0", "frag", deck="d", source_ids=["s1"]),
            edit("w1", a, "a1", tags=["y"]),
            CardPlan(wid="w2", action="keep", note_id=m, move_to="e"),
        ],
    )
    report, snap = workspace.apply(anki, store, plan)
    assert report.ok and report.undo_available
    new_id = report.created["w0"]
    assert anki.flags_of(a) == [0, 0]

    undone = workspace.undo(anki, store, snap)
    assert undone.ok and undone.errors == []
    assert new_id not in anki.notes
    assert store.anchors(new_id) == []
    assert anki.notes[a]["fields"] == {"Text": "a0", "Back Extra": "r"}
    assert anki.notes[a]["tags"] == ["x"]
    assert anki.flags_of(a) == [2, 0]
    assert anki.flags_of(m) == [1]
    assert anki.decks_of(m) == ["d"]
    assert store.anchors(m) == ["s1"], "anchors re-checked against the deck moved back to"


def test_undo_is_refused_without_a_snapshot_or_after_a_deletion(
    anki: FailingAnki, store: SourceStore
):
    with pytest.raises(workspace.NothingToUndo):
        workspace.undo(anki, store, None)
    gone = anki.add("d", flags=(1,))
    _, snap = workspace.apply(
        anki, store, ApplyPlan(deck="d", cards=[CardPlan(wid="w1", action="delete", note_id=gone)])
    )
    with pytest.raises(workspace.NothingToUndo):
        workspace.undo(anki, store, snap)


def test_undo_is_refused_when_a_note_changed_since_and_writes_nothing(
    anki: FailingAnki, store: SourceStore
):
    a = anki.add("d", fields={"Text": "a0", "Back Extra": ""}, flags=(1,))
    b = anki.add("d", fields={"Text": "b0", "Back Extra": ""}, flags=(1,))
    _, snap = workspace.apply(
        anki, store, ApplyPlan(deck="d", cards=[edit("w1", a, "a1"), edit("w2", b, "b1")])
    )
    anki.notes[b]["fields"]["Text"] = "edited in Anki meanwhile"
    before = len(anki.calls)
    with pytest.raises(workspace.ModifiedSince) as exc:
        workspace.undo(anki, store, snap)
    assert f"#{b}" in str(exc.value)
    assert anki.notes[a]["fields"]["Text"] == "a1", "nothing written"
    assert anki.calls[before:] == ["notesInfo"]


# ------------------------------------------------------------------------------- api


@pytest.fixture
def api(anki: FailingAnki, store: SourceStore) -> TestClient:
    app = FastAPI()
    app.include_router(routes_review.router, prefix="/api")
    app.include_router(routes_workspace.router, prefix="/api")
    app.state.anki = anki
    app.state.store = store
    app.state.last_validation = None
    return TestClient(app, raise_server_exceptions=False)


def test_api_apply_then_undo(api: TestClient, anki: FailingAnki):
    a = anki.add("d", fields={"Text": "a0", "Back Extra": "r"}, flags=(1,))
    assert api.get("/api/workspace/undo").json() == {"available": False}
    assert api.post("/api/workspace/undo").status_code == 409

    body = {
        "deck": "d",
        "cards": [
            {
                "wid": "w1",
                "action": "edit",
                "note_id": a,
                "fields": {"Text": "a1", "Back Extra": "r"},
            },
            {
                "wid": "w2",
                "action": "create",
                "model": "Cloze",
                "deck": "d",
                "fields": {"Text": "n"},
            },
        ],
    }
    res = api.post("/api/workspace/apply", json=body)
    assert res.status_code == 200
    report = res.json()
    assert report["ok"] is True and report["undo_available"] is True
    assert set(report["created"]) == {"w2"}
    assert anki.notes[a]["fields"] == {"Text": "a1", "Back Extra": ""}
    assert api.get("/api/workspace/undo").json() == {"available": True}

    res = api.post("/api/workspace/undo")
    assert res.status_code == 200 and res.json()["ok"] is True
    assert anki.notes[a]["fields"]["Text"] == "a0"
    assert anki.flags_of(a) == [1]
    assert api.get("/api/workspace/undo").json() == {"available": False}
    assert api.post("/api/workspace/undo").status_code == 409


def test_api_apply_maps_plan_and_note_errors(api: TestClient):
    assert api.post("/api/workspace/apply", json={"deck": "d", "cards": []}).status_code == 422
    res = api.post(
        "/api/workspace/apply",
        json={"deck": "d", "cards": [{"wid": "w1", "action": "keep", "note_id": 999}]},
    )
    assert res.status_code == 404


def test_api_failed_apply_is_a_200_report_and_keeps_no_snapshot(api: TestClient, anki: FailingAnki):
    a = anki.add("d", flags=(1,))
    anki.fail_on["updateNote"] = 1
    res = api.post(
        "/api/workspace/apply",
        json={
            "deck": "d",
            "cards": [{"wid": "w1", "action": "edit", "note_id": a, "fields": {"Text": "x"}}],
        },
    )
    assert res.status_code == 200
    report = res.json()
    assert report["ok"] is False and report["rolled_back"] is True
    assert api.get("/api/workspace/undo").json() == {"available": False}


def test_api_lookup_returns_known_notes_in_order(api: TestClient, anki: FailingAnki):
    a = anki.add("d", fields={"Text": "a"})
    b = anki.add("d", fields={"Text": "b"})
    res = api.post("/api/notes/lookup", json={"note_ids": [b, 999, a]})
    assert res.status_code == 200
    assert [n["note_id"] for n in res.json()] == [b, a]
    assert res.json()[0]["fields"]["Text"] == "b"


# ----------------------------------------------------------------------- model change


def test_apply_changes_note_type_via_update_note_model(anki: FailingAnki, store: SourceStore):
    """An edit with a different `model` calls updateNoteModel and the note becomes Basic."""
    nid = anki.add(
        "d", model="Cloze", fields={"Text": "old {{c1::x}}", "Back Extra": "r"}, flags=(1, 0)
    )
    plan = ApplyPlan(
        deck="d",
        cards=[
            CardPlan(
                wid="w1",
                action="edit",
                note_id=nid,
                model="Basic",
                fields={"Front": "question", "Back": "answer"},
                tags=["t"],
            )
        ],
    )
    report, snap = workspace.apply(anki, store, plan)
    assert report.ok
    assert anki.notes[nid]["modelName"] == "Basic"
    assert anki.notes[nid]["fields"] == {"Front": "question", "Back": "answer"}
    assert anki.notes[nid]["tags"] == ["t"]
    assert "updateNoteModel" in anki.calls
    assert "updateNote" not in anki.calls, "updateNoteModel replaces the normal edit"
    assert nid in snap.model_changed
    assert report.undo_available is True


def test_apply_model_change_rollback_restores_old_type(anki: FailingAnki, store: SourceStore):
    """On failure after a model change, rollback swaps back to the old type."""
    a = anki.add("d", model="Cloze", fields={"Text": "x", "Back Extra": ""}, flags=(1,))
    b = anki.add("d", model="Cloze", fields={"Text": "y", "Back Extra": ""}, flags=(1,))
    anki.fail_on["updateNoteModel"] = 2  # the second model change fails
    plan = ApplyPlan(
        deck="d",
        cards=[
            CardPlan(
                wid="w1",
                action="edit",
                note_id=a,
                model="Basic",
                fields={"Front": "q1", "Back": "a1"},
            ),
            CardPlan(
                wid="w2",
                action="edit",
                note_id=b,
                model="Basic",
                fields={"Front": "q2", "Back": "a2"},
            ),
        ],
    )
    report, snap = workspace.apply(anki, store, plan)
    assert report.ok is False
    assert report.rolled_back is True
    # The first note was rolled back to Cloze
    assert anki.notes[a]["modelName"] == "Cloze"
    assert anki.notes[a]["fields"] == {"Text": "x", "Back Extra": ""}


def test_undo_reverts_model_change(anki: FailingAnki, store: SourceStore):
    """Undo after a model change restores the original note type."""
    nid = anki.add("d", model="Cloze", fields={"Text": "old", "Back Extra": ""}, flags=(1,))
    plan = ApplyPlan(
        deck="d",
        cards=[
            CardPlan(
                wid="w1",
                action="edit",
                note_id=nid,
                model="Basic",
                fields={"Front": "q", "Back": "a"},
            )
        ],
    )
    report, snap = workspace.apply(anki, store, plan)
    assert report.ok and report.undo_available
    assert anki.notes[nid]["modelName"] == "Basic"

    undone = workspace.undo(anki, store, snap)
    assert undone.ok
    assert anki.notes[nid]["modelName"] == "Cloze"
    assert anki.notes[nid]["fields"] == {"Text": "old", "Back Extra": ""}


def test_apply_edit_without_model_change_uses_normal_path(anki: FailingAnki, store: SourceStore):
    """An edit where `model` matches the note's current type goes through `review.edit`."""
    nid = anki.add("d", model="Cloze", fields={"Text": "old", "Back Extra": ""}, flags=(1,))
    plan = ApplyPlan(
        deck="d",
        cards=[
            CardPlan(
                wid="w1",
                action="edit",
                note_id=nid,
                model="Cloze",
                fields={"Text": "new", "Back Extra": ""},
            )
        ],
    )
    report, snap = workspace.apply(anki, store, plan)
    assert report.ok
    assert "updateNoteModel" not in anki.calls
    assert "updateNote" in anki.calls
    assert nid not in snap.model_changed


# ---------------------------------------------------------------------------- defer


def test_validate_rejects_a_deferral_or_comment_in_the_wrong_place() -> None:
    plan = ApplyPlan(
        deck="d",
        cards=[
            CardPlan(wid="w1", action="keep", note_id=1, defer=True),
            CardPlan(wid="w2", action="keep", note_id=2, comment="x"),
            CardPlan(wid="w3", action="defer"),
        ],
    )
    joined = "\n".join(workspace.validate(plan))
    assert "w1 : seule une modification ou une création se diffère" in joined
    assert "w2 : un commentaire accompagne une note différée" in joined
    assert "w3 : note_id manquant" in joined
    fine = ApplyPlan(
        deck="d",
        cards=[
            CardPlan(wid="w1", action="defer", note_id=1, comment="plus tard"),
            edit("w2", 2, "x", defer=True, comment=""),
            create("w3", "new", defer=True, comment="à compléter"),
        ],
    )
    assert workspace.validate(fine) == []
    bad = ApplyPlan(deck="d", cards=[create("w1", "new", comment="x")])
    assert workspace.validate(bad) == ["w1 : un commentaire accompagne une note différée"]


def test_a_deferred_draft_is_created_flagged_with_its_comment(
    anki: FailingAnki, store: SourceStore
):
    plan = ApplyPlan(
        deck="d",
        cards=[
            create("w1", "frag", deck="d", defer=True, comment="à compléter\navec l'exemple"),
            create("w2", "other", deck="d"),
        ],
    )
    report, snap = workspace.apply(anki, store, plan)
    assert report.ok and report.errors == []
    new, other = report.created["w1"], report.created["w2"]
    assert anki.notes[new]["fields"] == {
        "Text": "frag",
        "Back Extra": "à compléter<br>avec l'exemple",
    }
    assert anki.flags_of(new) == [1]
    assert anki.flags_of(other) == [0]
    assert report.deferred == [new]
    assert snap.flagged == [], "a created note needs no flag restore: rollback deletes it"


def test_a_deferred_draft_gets_its_comment_even_when_the_proposal_left_the_field_out(
    anki: FailingAnki, store: SourceStore
):
    plan = ApplyPlan(
        deck="d",
        cards=[
            CardPlan(
                wid="w1",
                action="create",
                model="Cloze",
                fields={"Text": "t"},
                deck="d",
                defer=True,
                comment="à finir",
            ),
            CardPlan(
                wid="w2",
                action="create",
                model="Basic",
                fields={"Front": "f", "Back": "b"},
                deck="d",
                defer=True,
                comment="x",
            ),
        ],
    )
    report, _ = workspace.apply(anki, store, plan)
    assert report.ok
    assert anki.notes[report.created["w1"]]["fields"]["Back Extra"] == "à finir"
    assert anki.flags_of(report.created["w2"]) == [1]
    assert report.errors == ["w2 : pas de champ Back Extra, commentaire non écrit"]


def test_a_failure_after_a_deferred_draft_deletes_it(anki: FailingAnki, store: SourceStore):
    gone = anki.add("d", flags=(1,))
    anki.fail_on["deleteNotes"] = 1
    plan = ApplyPlan(
        deck="d",
        cards=[
            create("w1", "frag", deck="d", defer=True, comment="later"),
            CardPlan(wid="w2", action="delete", note_id=gone),
        ],
    )
    report, _ = workspace.apply(anki, store, plan)
    assert not report.ok and report.rolled_back
    assert report.created == {} and report.deferred == []
    assert len(anki.notes) == 1 and gone in anki.notes


def test_comment_html_escapes_and_keeps_line_breaks() -> None:
    assert workspace.comment_html(" a < b \n\n  c & d  ") == "a &lt; b<br><br>c &amp; d"
    assert workspace.comment_html("") == ""


def test_defer_keeps_the_flag_writes_the_comment_and_flags_an_unflagged_note(
    anki: FailingAnki, store: SourceStore
):
    root = anki.add("d", fields={"Text": "t", "Back Extra": "why"}, flags=(2, 0))
    pulled = anki.add("d", fields={"Text": "u", "Back Extra": ""}, flags=(0, 0))
    plan = ApplyPlan(
        deck="d",
        cards=[
            CardPlan(wid="w1", action="defer", note_id=root, comment="why\nand more"),
            CardPlan(wid="w2", action="defer", note_id=pulled, comment="à recouper", move_to="e"),
        ],
    )
    report, snap = workspace.apply(anki, store, plan)

    assert report.ok and report.errors == []
    assert anki.notes[root]["fields"] == {"Text": "t", "Back Extra": "why<br>and more"}
    assert anki.flags_of(root) == [2, 0], "an existing flag is kept as it was"
    assert anki.notes[pulled]["fields"]["Back Extra"] == "à recouper"
    assert anki.flags_of(pulled) == [1, 1], "a note that carried none gets a red flag everywhere"
    assert anki.decks_of(pulled) == ["e", "e"]
    assert report.deferred == [root, pulled]
    assert report.resolved == []
    assert report.moved == [pulled]
    assert report.undo_available is True
    assert snap.flagged == [pulled]
    assert snap.unflagged == []
    assert snap.written[root] == {"Back Extra": "why<br>and more"}


def test_edit_with_defer_writes_the_comment_instead_of_clearing_and_keeps_the_flag(
    anki: FailingAnki, store: SourceStore
):
    nid = anki.add("d", fields={"Text": "old", "Back Extra": "why"}, flags=(3,))
    plan = ApplyPlan(
        deck="d",
        clear_reason=True,
        cards=[edit("w1", nid, "new", defer=True, comment="mieux, mais pas fini")],
    )
    report, _ = workspace.apply(anki, store, plan)
    assert report.ok
    assert anki.notes[nid]["fields"] == {"Text": "new", "Back Extra": "mieux, mais pas fini"}
    assert anki.flags_of(nid) == [3]
    assert report.deferred == [nid] and report.resolved == []


def test_defer_without_comment_leaves_back_extra_alone(anki: FailingAnki, store: SourceStore):
    nid = anki.add("d", fields={"Text": "t", "Back Extra": "why"}, flags=(1,))
    report, snap = workspace.apply(
        anki, store, ApplyPlan(deck="d", cards=[CardPlan(wid="w1", action="defer", note_id=nid)])
    )
    assert report.ok and report.deferred == [nid]
    assert anki.notes[nid]["fields"]["Back Extra"] == "why"
    assert "updateNote" not in anki.calls
    assert snap.written == {}


def test_defer_on_a_note_type_without_back_extra_flags_and_reports(
    anki: FailingAnki, store: SourceStore
):
    nid = anki.add("d", model="Basic", fields={"Front": "f", "Back": "b"}, flags=(0,))
    plan = ApplyPlan(deck="d", cards=[CardPlan(wid="w1", action="defer", note_id=nid, comment="x")])
    report, _ = workspace.apply(anki, store, plan)
    assert report.ok
    assert anki.flags_of(nid) == [1]
    assert anki.notes[nid]["fields"] == {"Front": "f", "Back": "b"}
    assert report.errors == ["w1 : pas de champ Back Extra, commentaire non écrit"]


def test_undo_reverts_a_deferral(anki: FailingAnki, store: SourceStore):
    root = anki.add("d", fields={"Text": "t", "Back Extra": "why"}, flags=(2, 0))
    pulled = anki.add("d", fields={"Text": "u", "Back Extra": ""}, flags=(0,))
    plan = ApplyPlan(
        deck="d",
        cards=[
            CardPlan(wid="w1", action="defer", note_id=root, comment="later"),
            edit("w2", pulled, "u2", defer=True, comment="partiel"),
        ],
    )
    report, snap = workspace.apply(anki, store, plan)
    assert report.ok and anki.flags_of(pulled) == [1]

    undone = workspace.undo(anki, store, snap)
    assert undone.ok and undone.errors == []
    assert anki.notes[root]["fields"] == {"Text": "t", "Back Extra": "why"}
    assert anki.flags_of(root) == [2, 0]
    assert anki.notes[pulled]["fields"] == {"Text": "u", "Back Extra": ""}
    assert anki.flags_of(pulled) == [0], "the flag set by the deferral is removed"


def test_failure_after_a_deferral_rolls_its_flag_and_comment_back(
    anki: FailingAnki, store: SourceStore
):
    pulled = anki.add("d", fields={"Text": "u", "Back Extra": ""}, flags=(0,))
    gone = anki.add("d", flags=(1,))
    anki.fail_on["deleteNotes"] = 1
    plan = ApplyPlan(
        deck="d",
        cards=[
            CardPlan(wid="w1", action="defer", note_id=pulled, comment="later"),
            CardPlan(wid="w2", action="delete", note_id=gone),
        ],
    )
    report, _ = workspace.apply(anki, store, plan)
    assert not report.ok and report.rolled_back
    assert report.deferred == []
    assert anki.flags_of(pulled) == [0]
    assert anki.notes[pulled]["fields"]["Back Extra"] == ""
    assert gone in anki.notes


def test_api_accepts_a_deferred_plan(api: TestClient, anki: FailingAnki):
    nid = anki.add("d", fields={"Text": "t", "Back Extra": ""}, flags=(0,))
    body = {
        "deck": "d",
        "cards": [{"wid": "w1", "action": "defer", "note_id": nid, "comment": "plus tard"}],
    }
    r = api.post("/api/workspace/apply", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["deferred"] == [nid]
    assert anki.flags_of(nid) == [1]
    assert anki.notes[nid]["fields"]["Back Extra"] == "plus tard"
