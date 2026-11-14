"""Study days: the mobile deck's reviewed and planned cards per Anki day. Spec: specs/study.md.

Read only. Other local apps (the cockpit) ask for them over `/api/study/reviews`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from anki_assistant.client import AnkiClient
from anki_assistant.mobile import anki_day, from_epoch_ms

MAX_DAYS = 400
DEFAULT_DAYS = 90


@dataclass(frozen=True)
class StudyDay:
    day: date
    reviewed: int
    planned: int


@dataclass(frozen=True)
class StudyDays:
    deck: str
    rollover_hour: int
    today: date
    quota: int
    days: list[StudyDay]


def reviewed_per_day(answers: Iterable[tuple[int, int]], rollover_hour: int) -> dict[date, int]:
    """Distinct cards answered per Anki day, from (instant ms, card id) pairs."""
    cards: dict[date, set[int]] = {}
    for at, card_id in answers:
        cards.setdefault(anki_day(from_epoch_ms(at), rollover_hour), set()).add(card_id)
    return {day: len(ids) for day, ids in cards.items()}


def still_due_query(deck: str) -> str:
    """The deck's review cards due today or earlier that the phone would show."""
    quoted = '"' + deck.replace('"', '\\"') + '"'
    return f"deck:{quoted} is:review is:due -is:learn -is:suspended -is:buried flag:0"


def study_days(
    anki: AnkiClient, deck: str, rollover_hour: int, now: datetime, days: int = DEFAULT_DAYS
) -> StudyDays:
    """The last `days` Anki days of `deck`, today included, oldest first."""
    today = anki_day(now, rollover_hour)
    first = today - timedelta(days=days - 1)
    start = datetime.combine(first, datetime.min.time()) + timedelta(hours=rollover_hour)
    with anki.action():
        quota = anki.deck_limits(deck).review
        reviewed = reviewed_per_day(
            anki.review_answers(deck, int(start.timestamp() * 1000)), rollover_hour
        )
        due = len(anki.find_card_ids(still_due_query(deck)))
    listed = []
    for k in range(days):
        day = first + timedelta(days=k)
        count = reviewed.get(day, 0)
        planned = min(quota, count + due) if day == today else quota
        listed.append(StudyDay(day, count, planned))
    return StudyDays(deck, rollover_hour, today, quota, listed)
