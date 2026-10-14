"""The app's deck tree: the app deck order, labels and number prefixes.
Spec: specs/review.md#deck-tree-column-1.

Anki keeps every deck name; the app stores its own order and its labels in a JSON file
(default: `deck_order.json` next to the project, overridable with the ANKI_DECK_ORDER env var),
every deck named by its **deck id**, so a rename made in Anki desktop changes nothing here:

    {
      "order": {"1721677596007": [1743424601592, 1730823923348, ...]},
      "labels": {"l1": {"group": 1717010869088, "color": "magenta"},
                 "l2": {"name": "à revoir", "color": "cyan"}},
      "deck_labels": {"1734711885135": "l1"}
    }

- `order` maps a **tree parent** (a deck id, `0` for the top level) to the decks under it, in
  the app deck order. Only tree parents whose decks were reordered have a list.
- `labels` maps a label id to its color and either `group` (the deck id of a group turned into
  that label: the label reads the group's name) or `name` (a label made on a deck).
- `deck_labels` maps a deck id to the label set on that deck.

A **label** covers the deck that carries it and every deck under it, and labels never overlap.
A group turned into a label leaves the tree: its sub-decks take its place among the decks of
its own tree parent. The **tree parent** of a deck is its Anki parent, or that parent's tree
parent when the Anki parent is a group turned into a label.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

#: The label colors, picked in this order (each names an Omarchy palette token, theme.md).
PALETTE = ("magenta", "cyan", "green", "yellow", "blue", "red", "orange")
#: The tree parent of top-level decks.
ROOT = 0
#: The sub-deck that receives a group's own cards when it turns into a label.
MISC = "misc"

_PREFIX = re.compile(r"^\d+-(?=.)")

LabelHow = Literal["set", "group", "covered"]


def default_order_path() -> Path:
    env = os.environ.get("ANKI_DECK_ORDER")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2] / "deck_order.json"


def anki_key(name: str) -> tuple[str, ...]:
    """Sort key of a deck name in Anki's deck order: each `::` part, case ignored."""
    return tuple(part.casefold() for part in name.split("::"))


def hide_prefix(leaf: str) -> str:
    """A deck's own name without its number prefix (`01-AI` → `AI`); `01-` stays as it is."""
    return _PREFIX.sub("", leaf, count=1)


def _parent(name: str) -> str | None:
    return name.rsplit("::", 1)[0] if "::" in name else None


def _under(name: str, ancestor: str) -> bool:
    return name.startswith(ancestor + "::")


class LabelRefused(ValueError):
    """A label change that would make two labels overlap, or that the deck cannot take."""


# ---------------------------------------------------------------------- the store


@dataclass
class Label:
    id: str
    color: str
    group: int | None = None
    name: str = ""

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"color": self.color}
        if self.group is not None:
            out["group"] = self.group
        else:
            out["name"] = self.name
        return out


class DeckOrderStore:
    """Read/write the app deck order and the labels (the file described above)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path: Path | None = path or default_order_path()
        self.order: dict[int, list[int]] = {}
        self.labels: dict[str, Label] = {}
        self.deck_labels: dict[int, str] = {}
        self.load()

    @classmethod
    def in_memory(cls) -> DeckOrderStore:
        """An empty store tied to no file (Anki's deck order, no label), never saved."""
        store = cls.__new__(cls)
        store.path, store.order, store.labels, store.deck_labels = None, {}, {}, {}
        return store

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.order = {int(k): [int(d) for d in v] for k, v in (raw.get("order") or {}).items()}
        self.labels = {}
        for lid, entry in (raw.get("labels") or {}).items():
            group = entry.get("group")
            self.labels[str(lid)] = Label(
                id=str(lid),
                color=str(entry.get("color") or PALETTE[0]),
                group=int(group) if group is not None else None,
                name=str(entry.get("name") or ""),
            )
        self.deck_labels = {
            int(k): str(v) for k, v in (raw.get("deck_labels") or {}).items() if v in self.labels
        }

    def save(self) -> None:
        if self.path is None:
            return
        payload = {
            "order": {str(k): list(v) for k, v in sorted(self.order.items()) if v},
            "labels": {lid: label.to_dict() for lid, label in sorted(self.labels.items())},
            "deck_labels": {str(k): v for k, v in sorted(self.deck_labels.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def snapshot(self) -> str:
        return json.dumps(
            [
                sorted(self.order.items()),
                sorted((k, v.to_dict()) for k, v in self.labels.items()),
                sorted(self.deck_labels.items()),
            ],
            sort_keys=True,
            default=str,
        )

    def next_color(self) -> str:
        used = [label.color for label in self.labels.values()]
        for color in PALETTE:
            if color not in used:
                return color
        return PALETTE[len(used) % len(PALETTE)]

    def new_label_id(self) -> str:
        while True:
            lid = "l" + secrets.token_hex(3)
            if lid not in self.labels:
                return lid


# ----------------------------------------------------------------------- the tree


@dataclass
class LabelMark:
    """The label a row shows: `how` it reaches the deck — `set` on it, from the `group` it
    sits under, or `covered` by a label of a deck above."""

    id: str
    name: str
    color: str
    how: LabelHow


@dataclass
class TreeDeck:
    """One row of the app's tree."""

    id: int
    name: str
    #: The deck's own name, number prefix hidden.
    leaf: str
    #: Depth in the app's tree (0 for a top-level deck).
    depth: int
    #: The tree parent's deck id, `ROOT` (0) at the top level.
    parent_id: int
    has_subdecks: bool
    label: LabelMark | None = None


@dataclass
class LabelInfo:
    id: str
    name: str
    color: str
    #: The deck id of the group turned into this label, if any.
    group: int | None = None


@dataclass
class DeckTree:
    """The app's tree of every deck, read top to bottom."""

    rows: list[TreeDeck] = field(default_factory=list)
    labels: list[LabelInfo] = field(default_factory=list)
    #: Every deck name -> its place in the tree read top to bottom. A group turned into a
    #: label, absent from `rows`, takes the place right before its first deck.
    positions: dict[str, int] = field(default_factory=dict)
    #: Deck ids of the groups turned into labels.
    groups: set[int] = field(default_factory=set)
    by_name: dict[str, int] = field(default_factory=dict)

    def position(self, deck: str) -> int:
        return self.positions.get(deck, len(self.positions))


class _Decks:
    """Every deck of the collection, by name and by id, with its Anki children."""

    def __init__(self, decks: Iterable[tuple[str, int]]) -> None:
        self.ids: dict[str, int] = {}
        self.names: dict[int, str] = {}
        for name, did in decks:
            self.ids[name] = int(did)
            self.names[int(did)] = name
        self.children: dict[str | None, list[str]] = {}
        for name in sorted(self.ids, key=anki_key):
            self.children.setdefault(_parent(name), []).append(name)

    def ancestors(self, name: str) -> list[str]:
        """Nearest first."""
        out = []
        parent = _parent(name)
        while parent is not None:
            out.append(parent)
            parent = _parent(parent)
        return out


def _label_name(label: Label, decks: _Decks) -> str:
    if label.group is not None and label.group in decks.names:
        return hide_prefix(decks.names[label.group].split("::")[-1])
    return label.name


@dataclass
class _Carrier:
    deck: str
    how: Literal["group", "set"]
    label: str


def _carriers(store: DeckOrderStore, decks: _Decks) -> list[_Carrier]:
    out = [
        _Carrier(decks.names[label.group], "group", lid)
        for lid, label in store.labels.items()
        if label.group is not None and label.group in decks.names
    ]
    out += [
        _Carrier(decks.names[did], "set", lid)
        for did, lid in store.deck_labels.items()
        if did in decks.names
    ]
    return out


def _overlap(a: _Carrier, b: _Carrier) -> bool:
    return a.deck == b.deck or _under(a.deck, b.deck) or _under(b.deck, a.deck)


def _loser(a: _Carrier, b: _Carrier) -> _Carrier:
    """Of two overlapping carriers, the one removed: a group's label wins over a label set on
    a deck; between two of the same kind, the higher one wins."""
    if a.how != b.how:
        return a if a.how == "set" else b
    return b if _under(b.deck, a.deck) else a


def _drop(store: DeckOrderStore, carrier: _Carrier, decks: _Decks) -> None:
    if carrier.how == "set":
        store.deck_labels.pop(decks.ids[carrier.deck], None)
        return
    label = store.labels[carrier.label]
    if any(lid == carrier.label for lid in store.deck_labels.values()):
        # Still set on other decks: the label stays, under the name it read.
        label.name, label.group = _label_name(label, decks), None
    else:
        del store.labels[carrier.label]


def normalize(store: DeckOrderStore, decks: _Decks) -> None:
    """Bring the store in line with the collection after changes made in Anki
    (specs/review.md#labels): deleted decks leave it, a group's deletion takes its label, and
    labels that came to overlap lose one of them."""
    for lid, label in list(store.labels.items()):
        if label.group is not None and label.group not in decks.names:
            del store.labels[lid]
            for did, set_lid in list(store.deck_labels.items()):
                if set_lid == lid:
                    del store.deck_labels[did]
    for did in list(store.deck_labels):
        if did not in decks.names:
            del store.deck_labels[did]
    changed = True
    while changed:
        changed = False
        carriers = _carriers(store, decks)
        for i, a in enumerate(carriers):
            for b in carriers[i + 1 :]:
                if _overlap(a, b):
                    _drop(store, _loser(a, b), decks)
                    changed = True
                    break
            if changed:
                break
    carried = set(store.deck_labels.values())
    for lid, label in list(store.labels.items()):
        if label.group is None and lid not in carried:
            del store.labels[lid]
    for key in list(store.order):
        if key != ROOT and key not in decks.names:
            del store.order[key]


def _groups(store: DeckOrderStore, decks: _Decks) -> dict[str, str]:
    """Group name -> its label id, for the groups turned into labels."""
    return {
        decks.names[label.group]: lid
        for lid, label in store.labels.items()
        if label.group is not None and label.group in decks.names
    }


def _tree_parent(name: str, groups: Mapping[str, str]) -> str | None:
    parent = _parent(name)
    while parent is not None and parent in groups:
        parent = _parent(parent)
    return parent


def _default_children(parent: str | None, decks: _Decks, groups: Mapping[str, str]) -> list[str]:
    """The decks under a tree parent in Anki's deck order, a group turned into a label replaced
    by its own decks, in their order."""
    out: list[str] = []
    for child in decks.children.get(parent, []):
        if child in groups:
            out.extend(_ordered_children(child, decks, groups, None))
        else:
            out.append(child)
    return out


def _ordered_children(
    parent: str | None,
    decks: _Decks,
    groups: Mapping[str, str],
    store: DeckOrderStore | None,
) -> list[str]:
    default = _default_children(parent, decks, groups)
    stored = (store.order.get(_key(parent, decks)) if store is not None else None) or []
    present = set(default)
    head: list[str] = []
    for did in stored:
        name = decks.names.get(did)
        if name in present and name not in head:
            head.append(name)
    return head + [name for name in default if name not in head]


def _key(parent: str | None, decks: _Decks) -> int:
    return ROOT if parent is None else decks.ids[parent]


def _leaves(siblings: Sequence[str]) -> dict[str, str]:
    """Each sibling's shown name: number prefix hidden, unless two siblings would then read
    the same — those keep it."""
    hidden = {name: hide_prefix(name.split("::")[-1]) for name in siblings}
    counts: dict[str, int] = {}
    for shown in hidden.values():
        counts[shown.casefold()] = counts.get(shown.casefold(), 0) + 1
    return {
        name: shown if counts[shown.casefold()] == 1 else name.split("::")[-1]
        for name, shown in hidden.items()
    }


def _mark(
    name: str, store: DeckOrderStore, decks: _Decks, groups: Mapping[str, str]
) -> LabelMark | None:
    def info(lid: str, how: LabelHow) -> LabelMark:
        label = store.labels[lid]
        return LabelMark(id=lid, name=_label_name(label, decks), color=label.color, how=how)

    own = store.deck_labels.get(decks.ids[name])
    if own is not None:
        return info(own, "set")
    for depth, ancestor in enumerate(decks.ancestors(name)):
        if ancestor in groups:
            return info(groups[ancestor], "group" if depth == 0 else "covered")
        lid = store.deck_labels.get(decks.ids[ancestor])
        if lid is not None:
            return info(lid, "covered")
    return None


def build_tree(decks: Iterable[tuple[str, int]], store: DeckOrderStore) -> DeckTree:
    """The app's tree of `decks` ((name, deck id) pairs), the store brought in line with them
    first (and saved when that changed it)."""
    all_decks = _Decks(decks)
    before = store.snapshot()
    normalize(store, all_decks)
    if store.snapshot() != before:
        store.save()
    groups = _groups(store, all_decks)
    tree = DeckTree(groups={all_decks.ids[g] for g in groups}, by_name=dict(all_decks.ids))

    def walk(parent: str | None, depth: int) -> None:
        children = _ordered_children(parent, all_decks, groups, store)
        leaves = _leaves(children)
        for name in children:
            for ancestor in reversed(all_decks.ancestors(name)):
                if ancestor in groups and ancestor not in tree.positions:
                    tree.positions[ancestor] = len(tree.positions)
            tree.positions[name] = len(tree.positions)
            tree.rows.append(
                TreeDeck(
                    id=all_decks.ids[name],
                    name=name,
                    leaf=leaves[name],
                    depth=depth,
                    parent_id=_key(parent, all_decks),
                    has_subdecks=bool(all_decks.children.get(name)),
                    label=_mark(name, store, all_decks, groups),
                )
            )
            walk(name, depth + 1)

    walk(None, 0)
    for name in all_decks.ids:  # a group whose decks are all gone
        tree.positions.setdefault(name, len(tree.positions))
    tree.labels = [
        LabelInfo(id=lid, name=_label_name(label, all_decks), color=label.color, group=label.group)
        for lid, label in store.labels.items()
    ]
    return tree


# ---------------------------------------------------------------------- the changes


def tree_parent_of(tree: DeckTree, deck_id: int) -> int:
    for row in tree.rows:
        if row.id == deck_id:
            return row.parent_id
    raise KeyError(deck_id)


def move(
    store: DeckOrderStore,
    tree: DeckTree,
    deck_id: int,
    *,
    before: int | None = None,
    after: int | None = None,
) -> None:
    """Place a deck right before `before`, or right after `after`, among the decks of its tree
    parent (specs/review.md#moving-a-deck). Both must share the deck's tree parent: a drop
    elsewhere is refused (`ValueError`). With neither, the deck goes last."""
    parent = tree_parent_of(tree, deck_id)
    siblings = [row.id for row in tree.rows if row.parent_id == parent]
    target = before if before is not None else after
    if target is not None and target not in siblings:
        raise ValueError("A deck moves only among the decks of its tree parent")
    order = [d for d in siblings if d != deck_id]
    if target is None or target == deck_id:
        if target is None:
            order.append(deck_id)
        else:
            order = siblings
    else:
        index = order.index(target) + (1 if after is not None else 0)
        order.insert(index, deck_id)
    store.order[parent] = order
    store.save()


def _descendant_carriers(
    tree: DeckTree, store: DeckOrderStore, name: str
) -> list[tuple[str, str, str]]:
    """(deck name, label id, how) of every deck under `name` carrying a label."""
    out = []
    names = {did: n for n, did in tree.by_name.items()}
    for did, lid in store.deck_labels.items():
        deck = names.get(did)
        if deck is not None and _under(deck, name):
            out.append((deck, lid, "set"))
    for lid, label in store.labels.items():
        deck = names.get(label.group) if label.group is not None else None
        if deck is not None and _under(deck, name):
            out.append((deck, lid, "group"))
    return sorted(out, key=lambda c: tree.position(c[0]))


def _label_names(tree: DeckTree) -> dict[str, str]:
    return {label.id: label.name for label in tree.labels}


def _refusal(deck: str, tree: DeckTree, lid: str) -> str:
    leaf = deck.split("::")[-1]
    return f"{leaf} porte l'étiquette {_label_names(tree).get(lid, lid)}"


def _row(tree: DeckTree, deck_id: int) -> TreeDeck:
    for row in tree.rows:
        if row.id == deck_id:
            return row
    raise KeyError(deck_id)


def set_label(
    store: DeckOrderStore,
    tree: DeckTree,
    deck_id: int,
    *,
    label_id: str | None = None,
    new_name: str | None = None,
) -> str:
    """Set a label on a deck: an existing one (`label_id`) or a new one named `new_name`.
    Refused on a deck another label covers, and when a deck under it carries another label;
    a label set on a deck under it that is the same label is removed. Returns the label id."""
    row = _row(tree, deck_id)
    if row.label is not None and row.label.how != "set":
        raise LabelRefused(f"{row.leaf} a déjà l'étiquette {row.label.name}")
    name = (new_name or "").strip()
    if label_id is None:
        if not name:
            raise LabelRefused("Nom d'étiquette vide")
        existing = next((lab for lab in tree.labels if lab.name == name), None)
        label_id = existing.id if existing is not None else None
    elif label_id not in store.labels:
        raise KeyError(label_id)
    inner = _descendant_carriers(tree, store, row.name)
    for deck, lid, how in inner:
        if lid != label_id or how == "group":
            raise LabelRefused(_refusal(deck, tree, lid))
    if label_id is None:
        label_id = store.new_label_id()
        store.labels[label_id] = Label(id=label_id, color=store.next_color(), name=name)
    for deck, _, _ in inner:
        store.deck_labels.pop(tree.by_name[deck], None)
    store.deck_labels[deck_id] = label_id
    store.save()
    return label_id


def remove_label(store: DeckOrderStore, tree: DeckTree, deck_id: int) -> None:
    """Take off the label set on a deck; a label no deck carries any more goes with it."""
    lid = store.deck_labels.pop(deck_id, None)
    if lid is None:
        raise LabelRefused("Aucune étiquette posée sur ce paquet")
    label = store.labels.get(lid)
    if label is not None and label.group is None and lid not in store.deck_labels.values():
        del store.labels[lid]
    store.save()


def group_refusal(
    tree: DeckTree, store: DeckOrderStore, deck_id: int, protected: Iterable[str]
) -> str | None:
    """Why a deck cannot be turned into a label, or None when it can: it needs sub-decks, must
    not be the study deck or the mobile deck, nor be covered by a label, nor hold a deck
    carrying one."""
    row = _row(tree, deck_id)
    if not row.has_subdecks:
        return "Ce paquet n'a pas de sous-paquet"
    if row.name in set(protected):
        return f"{row.name} ne devient jamais une étiquette"
    if row.label is not None:
        return f"{row.leaf} a déjà l'étiquette {row.label.name}"
    inner = _descendant_carriers(tree, store, row.name)
    if inner:
        deck, lid, _ = inner[0]
        return _refusal(deck, tree, lid)
    return None


def turn_into_label(
    store: DeckOrderStore,
    tree: DeckTree,
    group_id: int,
    *,
    sub_decks: Sequence[int],
    misc_created: int | None = None,
) -> str:
    """Record a group as a label (its own cards and corpus already moved by the caller): its
    sub-decks (`sub_decks`, in their order under it) take its place among the decks of its
    tree parent; a `misc` sub-deck just created for its cards shows first among them."""
    parent = tree_parent_of(tree, group_id)
    lifted = list(sub_decks)
    if misc_created is not None:
        lifted = [misc_created] + [d for d in lifted if d != misc_created]
    if parent in store.order or misc_created is not None:
        siblings = [row.id for row in tree.rows if row.parent_id == parent]
        at = siblings.index(group_id)
        store.order[parent] = siblings[:at] + lifted + siblings[at + 1 :]
    store.order.pop(group_id, None)
    lid = store.new_label_id()
    store.labels[lid] = Label(id=lid, color=store.next_color(), group=group_id)
    store.save()
    return lid


__all__ = [
    "MISC",
    "PALETTE",
    "ROOT",
    "DeckOrderStore",
    "DeckTree",
    "Label",
    "LabelInfo",
    "LabelMark",
    "LabelRefused",
    "TreeDeck",
    "anki_key",
    "build_tree",
    "default_order_path",
    "group_refusal",
    "hide_prefix",
    "move",
    "normalize",
    "remove_label",
    "set_label",
    "turn_into_label",
]
