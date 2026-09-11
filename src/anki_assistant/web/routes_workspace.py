"""/api routes for the workspace: `POST /workspace/apply`, `POST /workspace/undo`.
Spec: specs/workspace.md#api.

The snapshot of the last successful validation lives on `app.state.last_validation` — one,
overwritten by the next validation, gone with the process.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from anki_assistant import workspace
from anki_assistant.web.errors import anki_errors
from anki_assistant.workspace import ApplyReport

router = APIRouter()


class CardIn(BaseModel):
    wid: str
    action: Literal["edit", "create", "delete", "keep", "defer"]
    note_id: int | None = None
    fields: dict[str, str] | None = None
    tags: list[str] | None = None
    model: str | None = None
    deck: str | None = None
    source_ids: list[str] | None = None
    move_to: str | None = None
    parent_wid: str | None = None
    defer: bool = False
    comment: str | None = None


class PlanIn(BaseModel):
    deck: str
    clear_reason: bool = True
    cards: list[CardIn] = Field(default_factory=list)

    def to_plan(self) -> workspace.ApplyPlan:
        return workspace.ApplyPlan(
            deck=self.deck,
            clear_reason=self.clear_reason,
            cards=[workspace.CardPlan(**card.model_dump()) for card in self.cards],
        )


@router.post("/workspace/apply")
def apply_workspace(request: Request, body: PlanIn) -> ApplyReport:
    """Write the plan all-or-nothing (emulated). 200 whether or not every write went through:
    the report's `ok` says. 422 on a malformed plan, 404 on an unknown note."""
    try:
        with anki_errors():
            report, snap = workspace.apply(
                request.app.state.anki, request.app.state.store, body.to_plan()
            )
    except workspace.PlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if report.ok:
        request.app.state.last_validation = snap
    return report


@router.post("/workspace/undo")
def undo_workspace(request: Request) -> ApplyReport:
    """Revert the last successful validation. 409 when there is none, when it deleted notes, or
    when a note was modified since."""
    snap = getattr(request.app.state, "last_validation", None)
    try:
        with anki_errors():
            report = workspace.undo(request.app.state.anki, request.app.state.store, snap)
    except (workspace.NothingToUndo, workspace.ModifiedSince) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    request.app.state.last_validation = None
    return report


@router.get("/workspace/undo")
def undo_status(request: Request) -> dict[str, bool]:
    """Whether « Annuler la dernière validation » has something to revert."""
    snap = getattr(request.app.state, "last_validation", None)
    return {"available": snap is not None and not snap.deleted}
