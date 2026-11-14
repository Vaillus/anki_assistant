# Study days

> How many cards of the mobile deck were reviewed each Anki day, out of how many were planned, for the other local apps that track Hugo's days (the cockpit's Savoir stat). Read-only.

## Reviewed cards

The **reviewed cards** of an Anki day are the cards of the [mobile deck](./mobile.md#phone-app) (sub-decks included, by the card's current deck) with at least one answer that day in Anki's review history as a **review**: a review card answered (revlog type `1`, ease 1–4). A card answered several times the same day (again, then good) counts once. New cards, learning cards and Anki's own rescheduling entries (ease `0`, « Set due date ») do not count.

An answer's Anki day is the [Anki day](./mobile.md#anki-day) of its instant in the review history: for a phone answer, the instant it was given on the phone ([timestamped replay](./mobile.md#sync)), not the sync. So a phone answer counts on its day once the phone has synced; an answer given in AnkiMobile counts once it has reached the Mac's collection (after Anki desktop's next AnkiWeb sync).

## Planned cards

The **review quota** is the mobile deck's effective review limit today (the `review` of the [daily quota](./mobile.md#batch)).

The **planned cards** of an Anki day:

- **today**: the review quota, or fewer when fewer were there to review: *min(review quota, reviewed today + review cards still due)*. The review cards still due are the mobile deck's review cards due today or earlier, neither suspended, buried nor flagged (a flagged card never reaches the phone).
- **a past day**: the review quota as it is today. Anki keeps no record of what was due on a past day.

## API

| Route | Effect |
|---|---|
| `GET /api/study/reviews?days=N` | The last `N` Anki days, today included (`N` from 1 to 400, default 90) |

```json
{
  "deck": "courant",
  "rollover_hour": 4,
  "today": "2026-11-14",
  "quota": 20,
  "days": [
    {"day": "2026-08-17", "reviewed": 18, "planned": 20},
    {"day": "2026-11-14", "reviewed": 12, "planned": 20}
  ]
}
```

`days` holds every day of the range, oldest first, today last; a day without reviews has `reviewed` 0. One action on the collection, read only: like any action, `503 Close Anki desktop first` while Anki desktop holds it ([00-overview.md § Anki](./00-overview.md#anki)). Main port only: the phone port does not serve it.
