"""Tests for deck_order.py: the app's tree over (name, deck id) pairs, with no collection."""

from __future__ import annotations

from pathlib import Path

import pytest

from anki_assistant import deck_order as do
from anki_assistant.deck_order import DeckOrderStore, LabelRefused, build_tree

NAMES = [
    "courant",
    "courant::00-Thèse",
    "courant::01-AI",
    "courant::01-AI::ML for CO",
    "courant::01-AI::On the measure of intelligence",
    "courant::01-AI::RL doesn't work yet",
    "courant::01-AI::little book of deep learning",
    "courant::01-AI::little book of deep learning::1",
    "courant::02-code",
    "courant::02-code::python",
    "courant::03-OR",
    "courant::03-OR::MCNF",
    "courant::03-OR::00-fundamentals",
    "dessin",
    "dessin révision du jour",
    "dessin::anatomie",
]
IDS = {name: 100 + i for i, name in enumerate(NAMES)}


def ids(*names: str) -> list[int]:
    return [IDS[n] for n in names]


@pytest.fixture
def store(tmp_path: Path) -> DeckOrderStore:
    return DeckOrderStore(tmp_path / "deck_order.json")


def tree(store: DeckOrderStore, names: list[str] | None = None) -> do.DeckTree:
    return build_tree([(n, IDS[n]) for n in names or NAMES], store)


def under(t: do.DeckTree, parent: str) -> list[str]:
    return [r.leaf for r in t.rows if r.parent_id == IDS[parent]]


def shown(t: do.DeckTree) -> list[str]:
    return ["  " * r.depth + r.leaf for r in t.rows]


def test_tree_starts_in_ankis_order_case_ignored_prefixes_hidden(store: DeckOrderStore):
    assert shown(tree(store)) == [
        "courant",
        "  Thèse",
        "  AI",
        "    little book of deep learning",
        "      1",
        "    ML for CO",
        "    On the measure of intelligence",
        "    RL doesn't work yet",
        "  code",
        "    python",
        "  OR",
        "    fundamentals",
        "    MCNF",
        "dessin",
        "  anatomie",
        "dessin révision du jour",
    ]


def test_two_siblings_reading_the_same_keep_their_prefix(store: DeckOrderStore):
    names = ["a", "a::01-AI", "a::AI", "a::01-"]
    t = build_tree([(n, i) for i, n in enumerate(names, 1)], store)
    assert sorted(r.leaf for r in t.rows if r.depth == 1) == ["01-", "01-AI", "AI"]


def test_move_places_a_deck_among_its_tree_parent_and_survives_unknown_decks(
    store: DeckOrderStore,
):
    t = tree(store)
    do.move(store, t, IDS["courant::03-OR"], before=IDS["courant::00-Thèse"])
    assert store.order[IDS["courant"]] == ids(
        "courant::03-OR", "courant::00-Thèse", "courant::01-AI", "courant::02-code"
    )
    # A deck created later goes after the ordered ones.
    IDS["courant::0-new"] = 999
    t = tree(store, [*NAMES, "courant::0-new"])
    assert under(t, "courant")[:5] == ["OR", "Thèse", "AI", "code", "new"]
    del IDS["courant::0-new"]

    reread = DeckOrderStore(store.path)
    assert reread.order == store.order, "saved to the file"


def test_move_after_and_refused_outside_tree_parent(store: DeckOrderStore):
    t = tree(store)
    do.move(store, t, IDS["courant::00-Thèse"], after=IDS["courant::02-code"])
    t = tree(store)
    assert under(t, "courant") == ["AI", "code", "Thèse", "OR"]
    with pytest.raises(ValueError):
        do.move(store, t, IDS["courant::00-Thèse"], before=IDS["courant::03-OR::MCNF"])


def test_group_turned_into_label_lifts_its_decks_into_its_place(store: DeckOrderStore):
    t = tree(store)
    group = IDS["courant::01-AI"]
    subs = [r.id for r in t.rows if r.parent_id == group]
    do.turn_into_label(store, t, group, sub_decks=subs)
    t = tree(store)
    assert shown(t)[:9] == [
        "courant",
        "  Thèse",
        "  little book of deep learning",
        "    1",
        "  ML for CO",
        "  On the measure of intelligence",
        "  RL doesn't work yet",
        "  code",
        "    python",
    ]
    rows = {r.name: r for r in t.rows}
    ml = rows["courant::01-AI::ML for CO"]
    assert ml.parent_id == IDS["courant"]
    assert ml.label is not None and (ml.label.name, ml.label.how) == ("AI", "group")
    chapter = rows["courant::01-AI::little book of deep learning::1"]
    assert chapter.label is not None and chapter.label.how == "covered"
    assert "courant::01-AI" not in rows
    assert (
        t.positions["courant::01-AI"] < t.positions["courant::01-AI::little book of deep learning"]
    )
    # A lifted deck now moves among courant's decks.
    do.move(store, t, ml.id, after=IDS["courant::03-OR"])
    t = tree(store)
    assert under(t, "courant")[-2:] == ["OR", "ML for CO"]


def test_group_turned_into_label_keeps_its_decks_reordered_order(store: DeckOrderStore):
    t = tree(store)
    group = IDS["courant::01-AI"]
    do.move(
        store,
        t,
        IDS["courant::01-AI::RL doesn't work yet"],
        before=IDS["courant::01-AI::little book of deep learning"],
    )
    t = tree(store)
    subs = [r.id for r in t.rows if r.parent_id == group]
    assert IDS["courant"] not in store.order, "courant was never reordered"
    do.turn_into_label(store, t, group, sub_decks=subs)
    t = tree(store)
    assert under(t, "courant")[:6] == [
        "Thèse",
        "RL doesn't work yet",
        "little book of deep learning",
        "ML for CO",
        "On the measure of intelligence",
        "code",
    ]
    assert group not in store.order


def test_misc_created_for_a_groups_cards_shows_first(store: DeckOrderStore):
    names = [*NAMES, "courant::03-OR::misc"]
    IDS["courant::03-OR::misc"] = 500
    try:
        t = tree(store, NAMES)
        group = IDS["courant::03-OR"]
        subs = [r.id for r in t.rows if r.parent_id == group]
        t = tree(store, names)
        do.turn_into_label(store, t, group, sub_decks=subs, misc_created=500)
        t = tree(store, names)
        assert under(t, "courant")[-3:] == ["misc", "fundamentals", "MCNF"]
    finally:
        del IDS["courant::03-OR::misc"]


def test_group_refusals(store: DeckOrderStore):
    t = tree(store)
    assert do.group_refusal(t, store, IDS["courant"], ["courant"]) is not None
    assert do.group_refusal(t, store, IDS["courant::00-Thèse"], []) is not None, "no sub-deck"
    assert do.group_refusal(t, store, IDS["courant::02-code"], []) is None
    do.set_label(store, t, IDS["courant::02-code::python"], new_name="AI")
    t = tree(store)
    assert do.group_refusal(t, store, IDS["courant::02-code"], []) == "python porte l'étiquette AI"


def test_set_label_colors_in_place_and_never_overlaps(store: DeckOrderStore):
    t = tree(store)
    python = IDS["courant::02-code::python"]
    lid = do.set_label(store, t, python, new_name="AI")
    t = tree(store)
    row = next(r for r in t.rows if r.id == python)
    assert row.label is not None and (row.label.how, row.label.color) == ("set", do.PALETTE[0])
    assert row.parent_id == IDS["courant::02-code"], "a set label changes no place"

    with pytest.raises(LabelRefused, match="python porte l'étiquette AI"):
        do.set_label(store, t, IDS["courant::02-code"], new_name="code")
    # The same label higher up takes over python's own.
    do.set_label(store, t, IDS["courant::02-code"], label_id=lid)
    t = tree(store)
    row = next(r for r in t.rows if r.id == python)
    assert row.label is not None and row.label.how == "covered"
    with pytest.raises(LabelRefused):
        do.set_label(store, t, python, new_name="autre")

    do.remove_label(store, t, IDS["courant::02-code"])
    assert store.labels == {}, "a label no deck carries any more is removed"


def test_changes_in_anki_resolve_overlapping_labels(store: DeckOrderStore):
    t = tree(store)
    do.set_label(store, t, IDS["dessin::anatomie"], new_name="x")
    group = IDS["courant::01-AI"]
    do.turn_into_label(store, t, group, sub_decks=[r.id for r in t.rows if r.parent_id == group])
    # In Anki, anatomie is moved under 01-AI (same id, new name): the group's label wins.
    moved = [n for n in NAMES if n != "dessin::anatomie"] + ["courant::01-AI::anatomie"]
    IDS["courant::01-AI::anatomie"] = IDS["dessin::anatomie"]
    try:
        t = tree(store, moved)
    finally:
        del IDS["courant::01-AI::anatomie"]
    row = next(r for r in t.rows if r.name == "courant::01-AI::anatomie")
    assert row.label is not None and (row.label.name, row.label.how) == ("AI", "group")
    assert store.deck_labels == {}


def test_deleted_group_takes_its_label(store: DeckOrderStore):
    t = tree(store)
    group = IDS["courant::01-AI"]
    do.turn_into_label(store, t, group, sub_decks=[r.id for r in t.rows if r.parent_id == group])
    t = tree(store, [n for n in NAMES if not n.startswith("courant::01-AI")])
    assert store.labels == {}
    assert t.labels == []
