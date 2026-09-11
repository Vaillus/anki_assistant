"""Validation of a workspace: plan, snapshot, ordered writes, rollback, undo.

Spec: specs/workspace.md.

Pure functions over an `AnkiClient` and a `SourceStore`, no FastAPI: `web/routes_workspace.py`
is the HTTP shell, the tests drive this module with the fake client of `tests/test_review.py`.

AnkiConnect has no transactions, so "all or nothing" is emulated:

1. the plan is validated before anything is written;
2. every existing note of the plan is read once into a `Snapshot`;
3. writes happen in an order where nothing is lost until the very last step
   (creates -> edits -> moves -> unflag / flag -> deletes);
4. on the first failure, whatever was written is put back from the snapshot (best effort), and
   the report says what failed and whether the rollback completed.

The snapshot of a successful validation is what `undo` replays later — the same restore path as
the rollback — unless the validation deleted notes, which cannot be recreated.
"""

from __future__ import annotations

import html
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from anki_assistant import review
from anki_assistant.client import AnkiClient
from anki_assistant.models import strip_html
from anki_assistant.review import REASON_FIELD, NoteNotFound
from anki_assistant.sources import SourceStore

__all__ = [
    "ACTIONS",
    "ApplyPlan",
    "ApplyReport",
    "CardPlan",
    "ModifiedSince",
    "NoteSnap",
    "NothingToUndo",
    "PlanError",
    "Snapshot",
    "anchors_after_move",
    "apply",
    "comment_html",
    "take_snapshot",
    "undo",
    "validate",
]

Action = Literal["edit", "create", "delete", "keep", "defer"]
ACTIONS: tuple[str, ...] = ("edit", "create", "delete", "keep", "defer")


class PlanError(ValueError):
    """The plan is malformed; nothing was written."""


class NothingToUndo(LookupError):
    """No validation to undo, or one that deleted notes."""


class ModifiedSince(RuntimeError):
    """A note no longer holds the values the validation wrote; nothing was written."""


# --------------------------------------------------------------------------- data


@dataclass
class CardPlan:
    """One card of the apply request (specs/workspace.md#api)."""

    wid: str
    action: str
    note_id: int | None = None
    #: Complete raw values of the shown version (edit) or of the new note (create).
    fields: dict[str, str] | None = None
    tags: list[str] | None = None
    model: str | None = None
    deck: str | None = None
    source_ids: list[str] | None = None
    move_to: str | None = None
    parent_wid: str | None = None
    #: Deferred (« à revoir »): the flag stays (or is set), `Back Extra` becomes `comment`.
    #: Implied by the action `defer`; a modifier on `edit` and `create`.
    defer: bool = False
    #: Plain text for `Back Extra`, only with a deferral. None leaves the field alone.
    comment: str | None = None

    @property
    def deferred(self) -> bool:
        return self.action == "defer" or (self.action in ("edit", "create") and self.defer)


@dataclass
class ApplyPlan:
    deck: str
    cards: list[CardPlan] = field(default_factory=list)
    #: Empty `Back Extra` on every edited note that had a reason.
    clear_reason: bool = True


@dataclass
class NoteSnap:
    """What it takes to put an existing note back: fields, tags, deck, model and flags."""

    note_id: int
    fields: dict[str, str]
    tags: list[str]
    deck: str
    model: str
    card_ids: list[int]
    #: card id -> flag (0 = none), for every card of the note.
    flags: dict[int, int]

    @property
    def flagged_card_ids(self) -> list[int]:
        return [cid for cid, flag in self.flags.items() if flag]


@dataclass
class Snapshot:
    """The state before a validation plus what the validation did, enough to undo it."""

    notes: dict[int, NoteSnap] = field(default_factory=dict)
    #: Note ids created, in creation order.
    created: list[int] = field(default_factory=list)
    #: Note ids deleted (an undo is refused when this is non-empty).
    deleted: list[int] = field(default_factory=list)
    #: Note ids whose cards were moved.
    moved: list[int] = field(default_factory=list)
    #: note id -> anchors before the move (a move can drop anchors; moving back restores them).
    anchors_before: dict[int, list[str]] = field(default_factory=dict)
    #: note id -> fields written, for the "modified since" check of undo.
    written: dict[int, dict[str, str]] = field(default_factory=dict)
    #: Note ids whose note type was changed (old model is in NoteSnap.model).
    model_changed: list[int] = field(default_factory=list)
    #: Note ids whose flags were cleared.
    unflagged: list[int] = field(default_factory=list)
    #: Note ids that carried no flag and were flagged by a deferral (every card, red).
    flagged: list[int] = field(default_factory=list)


@dataclass
class ApplyReport:
    """What a validation (or an undo) did. `ok` means every Anki write went through."""

    ok: bool = True
    #: wid -> note id, for every draft note that exists in Anki when the report is sent.
    created: dict[str, int] = field(default_factory=dict)
    resolved: list[int] = field(default_factory=list)
    #: Note ids left (or put) in the queue with their comment written.
    deferred: list[int] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    moved: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rolled_back: bool = False
    undo_available: bool = False


# ---------------------------------------------------------------------- validation


def validate(plan: ApplyPlan) -> list[str]:
    """Shape errors of a plan, in French since they are shown as-is. Empty when the plan is fine."""
    errors: list[str] = []
    if not plan.cards:
        errors.append("plan vide")
    seen_wids: set[str] = set()
    seen_notes: set[int] = set()
    for card in plan.cards:
        who = card.wid or "?"
        if not card.wid:
            errors.append("carte sans identifiant")
        elif card.wid in seen_wids:
            errors.append(f"identifiant {card.wid} en double")
        seen_wids.add(card.wid)
        if card.action not in ACTIONS:
            errors.append(f"{who} : action inconnue « {card.action} »")
            continue
        if card.action == "create":
            if card.note_id is not None:
                errors.append(f"{who} : une note à créer n'a pas de note_id")
            if not card.model:
                errors.append(f"{who} : type de note manquant")
            if not card.fields:
                errors.append(f"{who} : champs manquants")
            if card.move_to:
                errors.append(f"{who} : une note à créer ne se déplace pas")
            if card.comment is not None and not card.defer:
                errors.append(f"{who} : un commentaire accompagne une note différée")
            continue
        if card.note_id is None:
            errors.append(f"{who} : note_id manquant")
        elif card.note_id in seen_notes:
            errors.append(f"{who} : la note #{card.note_id} apparaît deux fois")
        else:
            seen_notes.add(card.note_id)
        if card.action == "edit" and not card.fields:
            errors.append(f"{who} : champs manquants")
        if card.action == "delete" and card.move_to:
            errors.append(f"{who} : une note supprimée ne se déplace pas")
        if card.defer and card.action not in ("edit", "create"):
            errors.append(f"{who} : seule une modification ou une création se diffère")
        if card.comment is not None and not card.deferred:
            errors.append(f"{who} : un commentaire accompagne une note différée")
    return errors


# ------------------------------------------------------------------------ snapshot


def take_snapshot(client: AnkiClient, plan: ApplyPlan) -> Snapshot:
    """Read every existing note of the plan in two round trips. Unknown note -> NoteNotFound."""
    ids = [card.note_id for card in plan.cards if card.action != "create" and card.note_id]
    snap = Snapshot()
    if not ids:
        return snap
    notes = {note.note_id: note for note in client.notes_info(ids)}
    missing = [nid for nid in ids if nid not in notes]
    if missing:
        raise NoteNotFound("note(s) introuvable(s) : " + ", ".join(f"#{n}" for n in missing))
    cards_by_note: dict[int, list] = defaultdict(list)
    for card in client.cards_info([cid for note in notes.values() for cid in note.card_ids]):
        cards_by_note[card.note_id].append(card)
    for nid in ids:
        note = notes[nid]
        cards = sorted(cards_by_note.get(nid, []), key=lambda c: c.ord)
        snap.notes[nid] = NoteSnap(
            note_id=nid,
            fields=dict(note.fields),
            tags=list(note.tags),
            deck=cards[0].deck_name if cards else plan.deck,
            model=note.model_name,
            card_ids=[c.card_id for c in cards] or list(note.card_ids),
            flags={c.card_id: c.flag for c in cards},
        )
    return snap


# --------------------------------------------------------------------------- apply


def anchors_after_move(store: SourceStore, note_id: int, deck: str) -> None:
    """Keep each anchor of the note only if its source is in the destination's effective corpus."""
    anchors = store.anchors(note_id)
    if not anchors:
        return
    in_destination = {source.id for source in store.corpus(deck)}
    store.set_anchors(note_id, [sid for sid in anchors if sid in in_destination])


def _reason_to_clear(snap: NoteSnap) -> bool:
    return bool(strip_html(snap.fields.get(REASON_FIELD, "")))


def comment_html(comment: str) -> str:
    """The `Back Extra` value for a plain-text comment: escaped, one `<br>` per line break."""
    return "<br>".join(
        html.escape(line.strip(), quote=False) for line in comment.strip().splitlines()
    )


def _comment_for(card: CardPlan, snap: NoteSnap, report: ApplyReport) -> str | None:
    """What a deferred card writes to `Back Extra`: the comment as HTML, or None when there is
    no comment or the note type has no such field (reported, not fatal: the flag still lands)."""
    if not card.deferred or card.comment is None:
        return None
    if REASON_FIELD not in snap.fields:
        if card.comment.strip():
            report.errors.append(f"{card.wid} : pas de champ {REASON_FIELD}, commentaire non écrit")
        return None
    return comment_html(card.comment)


def apply(client: AnkiClient, store: SourceStore, plan: ApplyPlan) -> tuple[ApplyReport, Snapshot]:
    """Write the plan in the safe order; on failure roll back and report. Never raises past
    validation and the snapshot (a malformed plan -> PlanError, an unknown note -> NoteNotFound)."""
    errors = validate(plan)
    if errors:
        raise PlanError(" ; ".join(errors))
    snap = take_snapshot(client, plan)
    report = ApplyReport()

    creates = [c for c in plan.cards if c.action == "create"]
    edits = [c for c in plan.cards if c.action == "edit"]
    keeps = [c for c in plan.cards if c.action == "keep"]
    defers = [c for c in plan.cards if c.action == "defer"]
    deletes = [c for c in plan.cards if c.action == "delete"]
    moves = [c for c in edits + keeps + defers if c.move_to]
    deferred = [c for c in edits + defers if c.deferred]

    step = ""
    try:
        for card in creates:
            step = f"création de {card.wid}"
            fields = dict(card.fields or {})
            if card.deferred and card.comment is not None:
                if REASON_FIELD in fields:
                    fields[REASON_FIELD] = comment_html(card.comment)
                elif card.comment.strip():
                    report.errors.append(
                        f"{card.wid} : pas de champ {REASON_FIELD}, commentaire non écrit"
                    )
            view = review.create(
                client,
                deck=card.deck or plan.deck,
                model=str(card.model),
                fields=fields,
                tags=list(card.tags or []),
            )
            snap.created.append(view.note_id)
            report.created[card.wid] = view.note_id
            if card.source_ids:
                store.set_anchors(view.note_id, card.source_ids)
            if card.deferred:
                step = f"flag de {card.wid}"
                client.set_flag(list(view.card_ids), 1)
                report.deferred.append(view.note_id)

        for card in edits:
            nid = int(card.note_id or 0)
            step = f"modification de #{nid}"
            fields = dict(card.fields or {})
            comment = _comment_for(card, snap.notes[nid], report)
            if comment is not None:
                fields[REASON_FIELD] = comment
            elif not card.deferred and plan.clear_reason and _reason_to_clear(snap.notes[nid]):
                fields[REASON_FIELD] = ""
            new_model = card.model
            old_model = snap.notes[nid].model
            if new_model and new_model != old_model:
                tags = list(card.tags) if card.tags is not None else list(snap.notes[nid].tags)
                client.update_note_model(nid, model=new_model, fields=fields, tags=tags)
                snap.model_changed.append(nid)
            else:
                review.edit(client, nid, fields=fields, tags=card.tags, unflag=False)
            snap.written[nid] = fields

        for card in defers:
            nid = int(card.note_id or 0)
            step = f"commentaire de #{nid}"
            comment = _comment_for(card, snap.notes[nid], report)
            if comment is not None:
                fields = {REASON_FIELD: comment}
                client.update_note(nid, fields=fields)
                snap.written[nid] = fields

        for card in moves:
            nid = int(card.note_id or 0)
            step = f"déplacement de #{nid}"
            snap.anchors_before[nid] = store.anchors(nid)
            client.change_deck(snap.notes[nid].card_ids, str(card.move_to))
            snap.moved.append(nid)
            report.moved.append(nid)
            anchors_after_move(store, nid, str(card.move_to))

        for card in edits + keeps:
            if card.deferred:
                continue
            nid = int(card.note_id or 0)
            step = f"levée du flag de #{nid}"
            flagged = snap.notes[nid].flagged_card_ids
            if flagged:
                client.clear_flag(flagged)
            snap.unflagged.append(nid)
            report.resolved.append(nid)

        for card in deferred:
            nid = int(card.note_id or 0)
            step = f"flag de #{nid}"
            note = snap.notes[nid]
            if not note.flagged_card_ids:
                client.set_flag(note.card_ids, 1)
                snap.flagged.append(nid)
            report.deferred.append(nid)

        if deletes:
            ids = [int(c.note_id or 0) for c in deletes]
            step = "suppression"
            client.delete_notes(ids)
            snap.deleted = ids
            report.deleted = ids
            try:
                store.remove_anchors(ids)
            except Exception as exc:  # noqa: BLE001 — sources.json is not Anki; report, keep ok
                report.errors.append(f"ancres des notes supprimées : {exc}")
    except Exception as exc:  # noqa: BLE001 — every failure is reported, never raised
        report.ok = False
        report.errors.append(f"{step} : {exc}")
        rollback_errors = _restore(client, store, snap, report)
        report.errors.extend(rollback_errors)
        report.rolled_back = not rollback_errors
        report.resolved = []
        report.deferred = []
        report.moved = []

    report.undo_available = report.ok and not snap.deleted
    return report, snap


# -------------------------------------------------------------------- restore / undo


def _restore(
    client: AnkiClient, store: SourceStore, snap: Snapshot, report: ApplyReport
) -> list[str]:
    """Put back what the snapshot recorded; returns the errors met (empty = complete).

    Runs the steps of `apply` in reverse. Each one is attempted even if a previous one failed.
    Created notes that could be deleted disappear from `report.created` (they no longer exist);
    the others stay, so that the client can keep their id.
    """
    errors: list[str] = []
    for nid in reversed(snap.created):
        try:
            client.delete_notes([nid])
            store.remove_anchors([nid])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"annulation de la création de #{nid} : {exc}")
        else:
            for wid, created_id in list(report.created.items()):
                if created_id == nid:
                    del report.created[wid]
    snap.created = [nid for nid in snap.created if nid in report.created.values()]

    for nid in snap.unflagged:
        note = snap.notes[nid]
        by_flag: dict[int, list[int]] = defaultdict(list)
        for cid, flag in note.flags.items():
            if flag:
                by_flag[flag].append(cid)
        try:
            for flag, cids in by_flag.items():
                client.set_flag(cids, flag)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"annulation de la levée du flag de #{nid} : {exc}")
    snap.unflagged = []

    for nid in snap.flagged:
        try:
            client.clear_flag(snap.notes[nid].card_ids)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"annulation du flag de #{nid} : {exc}")
    snap.flagged = []

    for nid in snap.moved:
        note = snap.notes[nid]
        try:
            client.change_deck(note.card_ids, note.deck)
            store.set_anchors(nid, snap.anchors_before.get(nid, []))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"annulation du déplacement de #{nid} : {exc}")
    snap.moved = []
    snap.anchors_before = {}

    for nid in list(snap.written):
        note = snap.notes[nid]
        try:
            if nid in snap.model_changed:
                client.update_note_model(
                    nid, model=note.model, fields=dict(note.fields), tags=list(note.tags)
                )
            else:
                client.update_note(nid, fields=dict(note.fields), tags=list(note.tags))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"annulation de la modification de #{nid} : {exc}")
    snap.written = {}
    snap.model_changed = []
    return errors


def undo(client: AnkiClient, store: SourceStore, snap: Snapshot | None) -> ApplyReport:
    """Revert the last validation from its snapshot (specs/workspace.md#undo).

    Refused (`NothingToUndo`) without a snapshot or when the validation deleted notes; refused
    (`ModifiedSince`) when an edited note no longer holds the values that were written — then
    nothing is written at all.
    """
    if snap is None:
        raise NothingToUndo("aucune validation à annuler")
    if snap.deleted:
        raise NothingToUndo("la dernière validation a supprimé des notes : annulation impossible")
    if snap.written:
        current = {note.note_id: note for note in client.notes_info(list(snap.written))}
        changed = [
            nid
            for nid, fields in snap.written.items()
            if nid not in current
            or any(str(current[nid].fields.get(k, "")) != str(v) for k, v in fields.items())
        ]
        if changed:
            raise ModifiedSince(
                ", ".join(f"#{n}" for n in changed) + " modifiée(s) depuis, annulation impossible"
            )
    report = ApplyReport(created={f"#{nid}": nid for nid in snap.created})
    errors = _restore(client, store, snap, report)
    report.created = {}
    report.errors = errors
    report.ok = not errors
    report.rolled_back = not errors
    return report
