"""/api/study routes: the mobile deck's study days, for other local apps. Spec: specs/study.md.

Main port only. The deck and rollover hour come from `app.state`, as for the phone routes.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query, Request

from anki_assistant import study
from anki_assistant.web.errors import anki_errors
from anki_assistant.web.routes_mobile import _anki, _deck, _rollover

router = APIRouter(prefix="/study")


@router.get("/reviews")
def reviews(
    request: Request, days: int = Query(study.DEFAULT_DAYS, ge=1, le=study.MAX_DAYS)
) -> dict:
    anki = _anki(request)
    with anki_errors(anki):
        out = study.study_days(anki, _deck(request), _rollover(request), datetime.now(), days)
    return {
        "deck": out.deck,
        "rollover_hour": out.rollover_hour,
        "today": out.today.isoformat(),
        "quota": out.quota,
        "days": [
            {"day": d.day.isoformat(), "reviewed": d.reviewed, "planned": d.planned}
            for d in out.days
        ],
    }
