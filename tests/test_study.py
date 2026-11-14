"""Study days (specs/study.md): reviewed and planned cards of the mobile deck per Anki day."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from anki.collection import Collection
from fastapi import FastAPI
from fastapi.testclient import TestClient

from anki_assistant import study
from anki_assistant.web import routes_study
from tests.test_mobile import DECK, NOW, TODAY, PhoneAnki, ms


@pytest.fixture
def anki(tmp_path: Path) -> PhoneAnki:
    client = PhoneAnki(tmp_path)
    client.set_limits(DECK, review=20, new=5)
    return client


def answer(anki: PhoneAnki, card_id: int, ease: int, at: datetime) -> None:
    assert anki.answer_card(card_id, ease, answered_at=ms(at), time_ms=1000) is not None


def days_of(anki: PhoneAnki, n: int = 3) -> dict[date, tuple[int, int]]:
    out = study.study_days(anki, DECK, 4, NOW, n)
    assert out.today == TODAY and out.days[-1].day == TODAY and len(out.days) == n
    return {d.day: (d.reviewed, d.planned) for d in out.days}


def test_reviewed_per_day_counts_each_card_once_per_anki_day() -> None:
    at = lambda s: ms(datetime.fromisoformat(s))  # noqa: E731
    answers = [
        (at("2026-11-14T09:00"), 1),
        (at("2026-11-14T09:01"), 1),  # again, then good: one card
        (at("2026-11-15T03:00"), 2),  # before the rollover: still the 14th
        (at("2026-11-15T05:00"), 1),
    ]
    assert study.reviewed_per_day(answers, 4) == {date(2026, 11, 14): 2, date(2026, 11, 15): 1}


def test_today_and_past_days(anki: PhoneAnki) -> None:
    done = [anki.new_card(rel_due=0) for _ in range(3)]
    anki.new_card(rel_due=-1)  # still due
    for cid in done:
        answer(anki, cid, 3, NOW)
    answer(anki, done[0], 1, NOW)  # answered twice today: still one card
    yesterday = anki.new_card(rel_due=0)
    answer(anki, yesterday, 3, NOW - timedelta(days=1))
    days = days_of(anki)
    assert days[TODAY] == (3, 4), "planned today: 3 reviewed + 1 still due"
    assert days[TODAY - timedelta(days=1)] == (1, 20), "a past day: the quota"
    assert days[TODAY - timedelta(days=2)] == (0, 20)
    assert anki.writes() == ["answer_card"] * 5


def test_planned_today_is_capped_by_the_quota(anki: PhoneAnki) -> None:
    anki.set_limits(DECK, review=2)
    for _ in range(4):
        anki.new_card(rel_due=-3)
    assert days_of(anki)[TODAY] == (0, 2)


def test_only_reviews_of_the_mobile_deck_count(anki: PhoneAnki) -> None:
    new = anki.new_card(queue=0)
    answer(anki, new, 3, NOW)
    other = anki.new_card(deck="autre", rel_due=0)
    answer(anki, other, 3, NOW)
    sub = anki.new_card(deck=f"{DECK}::sous-deck", rel_due=0)
    answer(anki, sub, 3, NOW)
    for skipped in (
        anki.new_card(rel_due=0, flags=2),
        anki.new_card(queue=-1),
        anki.new_card(rel_due=1),
    ):
        assert skipped
    assert days_of(anki)[TODAY] == (1, 1), "the sub-deck's review only; nothing else due"


@pytest.fixture
def http(anki: PhoneAnki) -> TestClient:
    app = FastAPI()
    app.state.anki = anki
    app.state.mobile_deck = DECK
    app.state.rollover_hour = 4
    app.include_router(routes_study.router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


def test_route_reviews(http: TestClient, anki: PhoneAnki) -> None:
    answer(anki, anki.new_card(rel_due=0), 3, NOW)
    body = http.get("/api/study/reviews?days=2").json()
    assert body == {
        "deck": DECK,
        "rollover_hour": 4,
        "today": TODAY.isoformat(),
        "quota": 20,
        "days": [
            {"day": (TODAY - timedelta(days=1)).isoformat(), "reviewed": 0, "planned": 20},
            {"day": TODAY.isoformat(), "reviewed": 1, "planned": 1},
        ],
    }
    assert (
        http.get("/api/study/reviews").json()["days"][0]["day"]
        == (TODAY - timedelta(days=study.DEFAULT_DAYS - 1)).isoformat()
    )
    assert http.get("/api/study/reviews?days=0").status_code == 422
    assert http.get("/api/study/reviews?days=401").status_code == 422


def test_route_reviews_while_anki_desktop_is_open(http: TestClient, anki: PhoneAnki) -> None:
    assert anki.path is not None
    desktop = Collection(str(anki.path))
    try:
        res = http.get("/api/study/reviews")
    finally:
        desktop.close()
    assert res.status_code == 503
    assert res.json()["detail"] == "Close Anki desktop first"
